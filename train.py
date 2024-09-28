import argparse
import functools
import os
import time
from datetime import timedelta

import paddle
import yaml
from loguru import logger
from paddle.io import DataLoader
from tqdm import tqdm
from visualdl import LogWriter

from data_utils.collate_fn import collate_fn
from data_utils.featurizer.audio_featurizer import AudioFeaturizer
from data_utils.featurizer.text_featurizer import TextFeaturizer
from data_utils.reader import CustomDataset
from data_utils.sampler import SortagradBatchSampler
from decoders.ctc_greedy_decoder import greedy_decoder_batch
from model_utils.model import DeepSpeech2Model
from utils.checkpoint import load_checkpoint, load_pretrained, save_checkpoint
from utils.metrics import wer, cer
from utils.scheduler import WarmupLR
from utils.utils import add_arguments, print_arguments, dict_to_object, labels_to_string

parser = argparse.ArgumentParser(description=__doc__)
add_arg = functools.partial(add_arguments, argparser=parser)
add_arg('use_gpu',          bool,   True,   "是否使用GPU训练")
add_arg('batch_size',       int,    8,      "训练每一批数据的大小")
add_arg('num_epoch',        int,    200,    "训练的轮数")
add_arg('num_rnn_layers',   int,    3,      "循环神经网络的数量")
add_arg('rnn_layer_size',   int,    1024,   "循环神经网络的大小")
add_arg('learning_rate',    float,  5e-4,   "初始学习率")
add_arg('min_duration',     float,  0.5,    "最短的用于训练的音频长度")
add_arg('max_duration',     float,  20.0,   "最长的用于训练的音频长度")
add_arg('resume_model',            str,  None,    "恢复训练，当为None则不使用预训练模型")
add_arg('pretrained_model',        str,  None,    "使用预训练模型的路径，当为None是不使用预训练模型")
add_arg('train_manifest',          str,  './dataset/manifest.train',     "训练的数据列表")
add_arg('test_manifest',           str,  './dataset/manifest.test',      "测试的数据列表")
add_arg('mean_istd_path',          str,  './dataset/mean_istd.json',     "均值和标准值得json文件路径，后缀 (.json)")
add_arg('vocab_path',              str,  './dataset/vocabulary.txt',     "数据集的词汇表文件路径")
add_arg('output_model_dir',        str,  './models/',                    "保存训练模型的文件夹")
add_arg('augment_conf_path',       str,  './conf/augmentation.yml',      "数据增强的配置文件，为json格式")
add_arg('metrics_type',            str,  'cer', "评估所使用的错误率方法，有字错率(cer)、词错率(wer)", choices=['wer', 'cer'])
args = parser.parse_args()
print_arguments(args=args)


# 训练模型
def train():
    # 是否使用GPU
    if args.use_gpu:
        assert paddle.is_compiled_with_cuda(), 'GPU不可用'
        paddle.device.set_device("gpu")
    else:
        os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
        paddle.device.set_device("cpu")

    # 读取数据增强配置文件
    with open(args.augment_conf_path, 'r', encoding='utf-8') as f:
        data_augment_configs = yaml.load(f.read(), Loader=yaml.FullLoader)
    print_arguments(configs=data_augment_configs, title='数据增强配置')
    data_augment_configs = dict_to_object(data_augment_configs)

    # 获取训练数据
    audio_featurizer = AudioFeaturizer(mode="train")
    text_featurizer = TextFeaturizer(args.vocab_path)
    train_dataset = CustomDataset(data_manifest=args.train_manifest,
                                  audio_featurizer=audio_featurizer,
                                  text_featurizer=text_featurizer,
                                  min_duration=args.min_duration,
                                  max_duration=args.max_duration,
                                  aug_conf=data_augment_configs,
                                  mode="train")
    train_batch_sampler = SortagradBatchSampler(train_dataset,
                                                batch_size=args.batch_size,
                                                sortagrad=True,
                                                drop_last=True,
                                                shuffle=True)
    train_loader = DataLoader(dataset=train_dataset,
                              collate_fn=collate_fn,
                              batch_sampler=train_batch_sampler,
                              num_workers=4)

    test_dataset = CustomDataset(data_manifest=args.test_manifest,
                                 audio_featurizer=audio_featurizer,
                                 text_featurizer=text_featurizer,
                                 min_duration=args.min_duration,
                                 max_duration=args.max_duration,
                                 aug_conf=data_augment_configs,
                                 mode="eval")
    test_loader = DataLoader(dataset=test_dataset,
                             collate_fn=collate_fn,
                             batch_size=args.batch_size,
                             num_workers=4)

    model = DeepSpeech2Model(input_dim=train_dataset.feature_dim,
                             vocab_size=train_dataset.vocab_size,
                             mean_istd_path=args.mean_istd_path,
                             num_rnn_layers=args.num_rnn_layers,
                             rnn_layer_size=args.rnn_layer_size)

    scheduler = WarmupLR(learning_rate=args.learning_rate)
    optimizer = paddle.optimizer.Adam(parameters=model.parameters(),
                                      learning_rate=scheduler,
                                      weight_decay=5e-4,
                                      grad_clip=paddle.nn.ClipGradByGlobalNorm(clip_norm=5.0))

    if args.pretrained_model:
        model = load_pretrained(args.pretrained_model, model)

    last_epoch, error_rate = 0, 1
    if args.resume_model:
        model, optimizer, last_epoch, error_rate = load_checkpoint(args.resume_model, model, optimizer)

    model.train()
    writer = LogWriter(logdir='log')
    ctc_loss = paddle.nn.CTCLoss()
    train_step = 0
    max_step = len(train_loader) * (args.num_epoch - last_epoch)
    for epoch_id in range(last_epoch, args.num_epoch):
        train_times, reader_times, batch_times, loss_sum = [], [], [], []
        start = time.time()
        start_epoch = time.time()
        for batch_id, batch in enumerate(train_loader()):
            inputs, labels, input_lens, label_lens = batch
            output, output_lens = model(inputs, input_lens)
            loss = ctc_loss(output, labels, output_lens, label_lens)
            loss.backward()
            optimizer.step()
            optimizer.clear_grad()
            scheduler.step()
            loss_sum.append(float(loss))
            train_times.append((time.time() - start) * 1000)
            # 记录学习率
            writer.add_scalar('Train/lr', scheduler.get_lr(), train_step)
            writer.add_scalar('Train/Loss', float(loss), train_step)
            train_step += 1

            # 多卡训练只使用一个进程打印
            if batch_id % 100 == 0:
                # 计算剩余时间
                train_eta_sec = (sum(train_times) / len(train_times)) * (max_step - train_step) / 1000
                eta_str = str(timedelta(seconds=int(train_eta_sec)))
                train_loss = sum(loss_sum) / len(loss_sum)
                logger.info(f'Train epoch: [{epoch_id + 1}/{args.num_epoch}], '
                            f'batch: [{batch_id}/{len(train_loader)}], '
                            f'loss: {train_loss:.5f}, '
                            f'learning_rate: {scheduler.get_lr():>.8f}, '
                            f'eta: {eta_str}')
                train_times, reader_times, batch_times, loss_sum = [], [], [], []
            start = time.time()

        train_time_str = str(timedelta(seconds=int(time.time() - start_epoch)))
        error_result = evaluate(model, test_loader, text_featurizer)
        writer.add_scalar(f'Test/{args.metrics_type}', error_result, epoch_id)
        logger.info(f'Test epoch: {epoch_id + 1}，训练耗时：{train_time_str}, {args.metrics_type}: {error_result}')

        save_checkpoint(model, optimizer, epoch_id, save_model_path=args.output_model_dir,
                        error_rate=error_result, metrics_type=args.metrics_type)


def evaluate(model, test_loader, text_featurizer):
    model.eval()
    error_results = []
    with paddle.no_grad():
        for batch_id, batch in enumerate(tqdm(test_loader())):
            inputs, labels, input_lens, label_lens = batch
            output = model.predict(inputs, input_lens).numpy()
            out_strings = greedy_decoder_batch(output, text_featurizer.vocab_list)
            labels_str = labels_to_string(labels, text_featurizer.vocab_list)
            for out_string, label in zip(*(out_strings, labels_str)):
                # 计算字错率或者词错率
                if args.metrics_type == 'wer':
                    error_rate = wer(label, out_string)
                else:
                    error_rate = cer(label, out_string)
                error_results.append(error_rate)
    error_result = float(sum(error_results) / len(error_results)) if len(error_results) > 0 else -1
    model.train()
    return error_result


if __name__ == '__main__':
    train()
