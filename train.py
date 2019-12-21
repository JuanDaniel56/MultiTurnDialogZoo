#!/usr/bin/python3
# Author: GMFTBY
# Time: 2019.9.15

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.nn.utils import clip_grad_norm_
import torch.optim as optim
import random
import numpy as np
import argparse
import math
import pickle
from tqdm import tqdm
from torch.utils.tensorboard import SummaryWriter
import ipdb

from utils import *
from data_loader import *
from metric.metric import *
from model.HRED import HRED
from model.seq2seq_attention import Seq2Seq
from model.WSeq import WSeq
from model.ReCoSa import ReCoSa
from model.seq2seq_transformer import transformer
from model.MReCoSa import MReCoSa
from model.MTGCN import MTGCN
from model.GCNRNN import GCNRNN
from model.GatedGCN import GatedGCN
from model.layers import *


def train(train_iter, net, optimizer, vocab_size, pad, 
          grad_clip=10, graph=False, debug=False, transformer_decode=False):
    # choose nll_loss for training the objective function
    net.train()
    total_loss, batch_num = 0.0, 0
    criterion = nn.NLLLoss(ignore_index=pad)

    pbar = tqdm(train_iter)

    for idx, batch in enumerate(pbar):
        # [turn, length, batch], [seq_len, batch] / [seq_len, batch], [seq_len, batch]
        if graph:
            sbatch, tbatch, gbatch, subatch, tubatch, turn_lengths = batch
        else:
            sbatch, tbatch, turn_lengths = batch
        batch_size = tbatch.shape[1]
        if batch_size == 1:
            # batchnorm will throw error when batch_size is 1
            continue

        if isinstance(optimizer, NoamOpt):
            optimizer.optimizer.zero_grad()
        else:
            optimizer.zero_grad()

        # [seq_len, batch, vocab_size]
        if graph:
            output = net(sbatch, tbatch, gbatch, subatch, tubatch, turn_lengths)
        else:
            output = net(sbatch, tbatch, turn_lengths)
        
        if transformer_decode:
            loss = criterion(output[:-1].view(-1, vocab_size),
                             tbatch[1:].contiguous().view(-1))
        else:
            loss = criterion(output[1:].view(-1, vocab_size),
                             tbatch[1:].contiguous().view(-1))

        # add train loss to the tensorfboard
        # writer.add_scalar(f'{writer_str}-Loss/train-{epoch}', loss, idx)

        loss.backward()
        clip_grad_norm_(net.parameters(), grad_clip)

        optimizer.step()
        total_loss += loss.item()
        batch_num += 1

        pbar.set_description(f'batch {batch_num}, training loss: {round(loss.item(), 4)}')

        if debug:
            # show the output result, output: [length, batch, vocab_size]
            ipdb.set_trace()
            utterance = output[:, 0, :].squeeze(1)    # [length, vocab_size]
            word_idx = utterance.data.max(1)[1]       # [length]

    # return avg loss
    return round(total_loss / batch_num, 4)


def validation(data_iter, net, vocab_size, pad, 
               graph=False, transformer_decode=False, debug=False):
    net.eval()
    batch_num, total_loss = 0, 0.0
    criterion = nn.NLLLoss(ignore_index=pad)

    pbar = tqdm(data_iter)

    for idx, batch in enumerate(pbar):
        if graph:
            sbatch, tbatch, gbatch, subatch, tubatch, turn_lengths = batch
        else:
            sbatch, tbatch, turn_lengths = batch
        batch_size = tbatch.shape[1]
        if batch_size == 1:
            continue

        if graph:
            output = net(sbatch, tbatch, gbatch, subatch, tubatch, turn_lengths)
        else:
            output = net(sbatch, tbatch, turn_lengths)

        if transformer_decode:
            loss = criterion(output[:-1].view(-1, vocab_size),
                             tbatch[1:].contiguous().view(-1))
        else:
            loss = criterion(output[1:].view(-1, vocab_size),
                             tbatch[1:].contiguous().view(-1))
        total_loss += loss.item()
        batch_num += 1
        
        pbar.set_description(f'batch {idx}, dev/test loss: {round(loss.item(), 4)}')

        if debug:
            # show the output result, output: [length, batch, vocab_size]
            ipdb.set_trace()
            utterance = output[:, 0, :].squeeze(1)    # [length, vocab_size]
            word_idx = utterance.data.max(1)[1]       # [length]

    return round(total_loss / batch_num, 4)


def test(data_iter, net, vocab_size, pad, debug=False, graph=False):
    test_loss = validation(data_iter, net, vocab_size, pad, debug=debug, graph=graph)
    return test_loss


def translate(data_iter, net, **kwargs):
    net.eval()
    # load the vocab
    tgt_vocab = load_pickle(kwargs['tgt_vocab'])
    src_vocab = load_pickle(kwargs['src_vocab'])
    src_w2idx, src_idx2w = src_vocab
    tgt_w2idx, tgt_idx2w = tgt_vocab
    
    # calculate the loss
    criterion = nn.NLLLoss(ignore_index=tgt_w2idx['<pad>'])
    total_loss, batch_num = 0.0, 0
    
    # translate, which is the same as the translate.py
    with open(kwargs['pred'], 'w') as f:
        pbar = tqdm(data_iter)
        for batch in pbar:
            if kwargs['graph'] == 1:
                sbatch, tbatch, gbatch, subatch, tubatch, turn_lengths = batch
            else:
                sbatch, tbatch, turn_lengths = batch

            batch_size = tbatch.shape[1]
            if kwargs['hierarchical']:
                turn_size = len(sbatch)
            
            src_pad, tgt_pad = src_w2idx['<pad>'], tgt_w2idx['<pad>']
            src_eos, tgt_eos = src_w2idx['<eos>'], tgt_w2idx['<eos>']
            
            # output: [maxlen, batch_size], sbatch: [turn, max_len, batch_size]
            if kwargs['graph'] == 1:
                output, f_l = net.predict(sbatch, gbatch, 
                                          subatch, tubatch, 
                                          len(tbatch), turn_lengths,
                                          loss=True)
            else:
                output, f_l = net.predict(sbatch, len(tbatch), turn_lengths,
                                          loss=True)
                        
            # ipdb.set_trace()
            loss = criterion(f_l[1:].view(-1, len(tgt_w2idx)),
                             tbatch[1:].contiguous().view(-1))
            batch_num += 1
            total_loss += loss.item()
            
            for i in range(batch_size):
                ref = list(map(int, tbatch[:, i].tolist()))
                tgt = list(map(int, output[:, i].tolist()))    # [maxlen]
                if kwargs['hierarchical']:
                    src = [sbatch[j][:, i].tolist() for j in range(turn_size)]   # [turns, maxlen]
                else:
                    src = list(map(int, sbatch[:, i].tolist()))

                # filte the <pad>
                ref_endx = ref.index(tgt_pad) if tgt_pad in ref else len(ref)
                ref_endx_ = ref.index(tgt_eos) if tgt_eos in ref else len(ref)
                ref_endx = min(ref_endx, ref_endx_)
                ref = ref[1:ref_endx]
                ref = ' '.join(num2seq(ref, tgt_idx2w))
                ref = ref.replace('<sos>', '').strip()
                ref = ref.replace('< user1 >', '').strip()
                ref = ref.replace('< user0 >', '').strip()

                tgt_endx = tgt.index(tgt_pad) if tgt_pad in tgt else len(tgt)
                tgt_endx_ = tgt.index(tgt_eos) if tgt_eos in tgt else len(tgt)
                tgt_endx = min(tgt_endx, tgt_endx_)
                tgt = tgt[1:tgt_endx]
                tgt = ' '.join(num2seq(tgt, tgt_idx2w))
                tgt = tgt.replace('<sos>', '').strip()
                tgt = tgt.replace('< user1 >', '').strip()
                tgt = tgt.replace('< user0 >', '').strip()

                if kwargs['hierarchical']:
                    source = []
                    for item in src:
                        item_endx = item.index(src_pad) if src_pad in item else len(item)
                        item_endx_ = item.index(src_eos) if src_eos in item else len(item)
                        item_endx = min(item_endx, item_endx_)
                        item = item[1:item_endx]
                        item = num2seq(item, src_idx2w)
                        source.append(' '.join(item))
                    src = ' __eou__ '.join(source)
                else:
                    src_endx = src.index(src_pad) if src_pad in src else len(src)
                    src_endx_ = src.index(src_eos) if src_eos in src else len(src)
                    sec_endx = min(src_endx, src_endx_)
                    src = src[1:src_endx]
                    src = ' '.join(num2seq(src, src_idx2w))

                f.write(f'- src: {src}\n')
                f.write(f'- ref: {ref}\n')
                f.write(f'- tgt: {tgt}\n\n')
                        
    l = round(total_loss / batch_num, 4)
    print(f'[!] write the translate result into {kwargs["pred"]}')
    print(f'[!] loss: {l}, ppl: {round(math.exp(l), 4)}', 
          file=open(f'./processed/{kwargs["dataset"]}/{kwargs["model"]}/ppl.txt', 'a'))
    
    return math.exp(l)

    
def write_into_tb(pred_path, writer, writer_str, epoch, ppl):
    # obtain the performance
    with open(pred_path) as f:
        ref, tgt = [], []
        for idx, line in enumerate(f.readlines()):
            if idx % 4 == 1:
                line = line.replace("user1", "").replace("user0", "").replace("- ref: ", "").replace('<sos>', '').replace('<eos>', '').strip()
                ref.append(line.split())
            elif idx % 4 == 2:
                line = line.replace("user1", "").replace("user0", "").replace("- tgt: ", "").replace('<sos>', '').replace('<eos>', '').strip()
                tgt.append(line.split())

    assert len(ref) == len(tgt)

    # BLEU and ROUGE
    rouge_sum, bleu1_sum, bleu2_sum, bleu3_sum, bleu4_sum, counter = 0, 0, 0, 0, 0, 0
    for rr, cc in tqdm(list(zip(ref, tgt))):
        rouge_sum += cal_ROUGE(rr, cc)
        bleu1_sum += cal_BLEU([rr], cc, ngram=1)
        bleu2_sum += cal_BLEU([rr], cc, ngram=2)
        bleu3_sum += cal_BLEU([rr], cc, ngram=3)
        bleu4_sum += cal_BLEU([rr], cc, ngram=4)
        counter += 1

    # Distinct-1, Distinct-2
    candidates, references = [], []
    for line1, line2 in zip(tgt, ref):
        candidates.extend(line1)
        references.extend(line2)
    distinct_1, distinct_2 = cal_Distinct(candidates)
    rdistinct_1, rdistinct_2 = cal_Distinct(references)

    # BERTScore < 512 for bert
    # Fuck BERTScore, slow as the snail, fuck it
    # ref = [' '.join(i) for i in ref]
    # tgt = [' '.join(i) for i in tgt]
    # bert_scores = cal_BERTScore(ref, tgt)
    
    # Embedding-based metric: Embedding Average (EA), Vector Extrema (VX), Greedy Matching (GM)
    # load the dict
    with open('./data/glove_embedding.pkl', 'rb') as f:
        dic = pickle.load(f)
    ea_sum, vx_sum, gm_sum, counterp = 0, 0, 0, 0
    for rr, cc in tqdm(list(zip(ref, tgt))):
        ea_sum += cal_embedding_average(rr, cc, dic)
        vx_sum += cal_vector_extrema(rr, cc, dic)
        # gm_sum += cal_greedy_matching(rr, cc, dic)
        counterp += 1
        
    # write into the tensorboard
    writer.add_scalar(f'{writer_str}-Performance/PPL', ppl, epoch)
    writer.add_scalar(f'{writer_str}-Performance/BLEU-1', bleu1_sum / counter, epoch)
    writer.add_scalar(f'{writer_str}-Performance/BLEU-2', bleu2_sum / counter, epoch)
    writer.add_scalar(f'{writer_str}-Performance/BLEU-3', bleu3_sum / counter, epoch)
    writer.add_scalar(f'{writer_str}-Performance/BLEU-4', bleu4_sum / counter, epoch)
    writer.add_scalar(f'{writer_str}-Performance/ROUGE', rouge_sum / counter, epoch)
    writer.add_scalar(f'{writer_str}-Performance/Distinct-1', distinct_1, epoch)
    writer.add_scalar(f'{writer_str}-Performance/Distinct-2', distinct_2, epoch)
    writer.add_scalar(f'{writer_str}-Performance/Ref-Distinct-1', rdistinct_1, epoch)
    writer.add_scalar(f'{writer_str}-Performance/Ref-Distinct-2', rdistinct_2, epoch)
    writer.add_scalar(f'{writer_str}-Performance/Embedding-Average', ea_sum / counterp, epoch)
    writer.add_scalar(f'{writer_str}-Performance/Vector-Extrema', vx_sum / counterp, epoch)


def main(**kwargs):
    # tensorboard 
    writer = SummaryWriter(log_dir=f'./tblogs/{kwargs["dataset"]}/{kwargs["model"]}')

    # load vocab
    src_vocab, tgt_vocab = load_pickle(kwargs['src_vocab']), load_pickle(kwargs['tgt_vocab'])
    src_w2idx, src_idx2w = src_vocab
    tgt_w2idx, tgt_idx2w = tgt_vocab

    # pretrained path
    if kwargs['pretrained'] == 'bert':
        pretrained = f'./processed/{kwargs["dataset"]}/{kwargs["model"]}/'
    else:
        pretrained = None

    # create the net
    if kwargs['model'] == 'HRED':
        net = HRED(kwargs['embed_size'], len(src_w2idx), len(tgt_w2idx),
                   kwargs['utter_hidden'], kwargs['context_hidden'],
                   kwargs['decoder_hidden'], teach_force=kwargs['teach_force'],
                   pad=tgt_w2idx['<pad>'], sos=tgt_w2idx['<sos>'], 
                   utter_n_layer=kwargs['utter_n_layer'], dropout=kwargs['dropout'],
                   pretrained=pretrained)
    elif kwargs['model'] == 'WSeq':
        net = WSeq(kwargs['embed_size'], len(src_w2idx), len(tgt_w2idx),
                   kwargs['utter_hidden'], kwargs['context_hidden'],
                   kwargs['decoder_hidden'], teach_force=kwargs['teach_force'],
                   pad=tgt_w2idx['<pad>'], sos=tgt_w2idx['<sos>'], 
                   utter_n_layer=kwargs['utter_n_layer'], dropout=kwargs['dropout'],
                   pretrained=pretrained)
    elif kwargs['model'] == 'ReCoSa':
        net = ReCoSa(len(src_w2idx), kwargs['d_model'], kwargs['d_model'], len(tgt_w2idx),
                     dropout=kwargs['dropout'], n_layers=kwargs['utter_n_layer'],
                     sos=tgt_w2idx['<sos>'], pad=tgt_w2idx['<pad>'],
                     pretrained=pretrained)
    elif kwargs['model'] == 'Transformer':
        net = transformer(len(src_w2idx), len(tgt_w2idx), embed_size=kwargs['d_model'],
                          nhead=8, n_layers=6, src_pad=src_w2idx['<pad>'],
                          tgt_pad=tgt_w2idx['<pad>'], tgt_sos=tgt_w2idx['<sos>'],
                          dropout=kwargs['dropout'], pretrained=pretrained)
    elif kwargs['model'] == 'MReCoSa':
        net = MReCoSa(len(src_w2idx), 512, len(tgt_w2idx), 512, 512,
                      teach_force=kwargs['teach_force'], pad=tgt_w2idx['<pad>'],
                      sos=tgt_w2idx['<sos>'], dropout=kwargs['dropout'],
                      utter_n_layer=kwargs['utter_n_layer'], pretrained=pretrained)
    elif kwargs['model'] == 'Seq2Seq':
        net = Seq2Seq(len(src_w2idx), kwargs['embed_size'], len(tgt_w2idx), 
                      kwargs['utter_hidden' ], 
                      kwargs['decoder_hidden'], teach_force=kwargs['teach_force'],
                      pad=tgt_w2idx['<pad>'], sos=tgt_w2idx['<sos>'],
                      dropout=kwargs['dropout'], 
                      utter_n_layer=kwargs['utter_n_layer'], pretrained=pretrained)
    elif kwargs['model'] == 'MTGCN':
        net = MTGCN(len(src_w2idx), len(tgt_w2idx), kwargs['embed_size'], 
                    kwargs['utter_hidden'], kwargs['context_hidden'],
                    kwargs['decoder_hidden'], kwargs['position_embed_size'], 
                    teach_force=kwargs['teach_force'], pad=tgt_w2idx['<pad>'], 
                    sos=tgt_w2idx['<sos>'], dropout=kwargs['dropout'],
                    utter_n_layer=kwargs['utter_n_layer'],
                    context_threshold=kwargs['context_threshold'])
    elif kwargs['model'] == 'GCNRNN':
        net = GCNRNN(len(src_w2idx), len(tgt_w2idx), kwargs['embed_size'], 
                     kwargs['utter_hidden'], kwargs['context_hidden'],
                     kwargs['decoder_hidden'], kwargs['position_embed_size'], 
                     teach_force=kwargs['teach_force'], pad=tgt_w2idx['<pad>'], 
                     sos=tgt_w2idx['<sos>'], dropout=kwargs['dropout'],
                     utter_n_layer=kwargs['utter_n_layer'])
    elif kwargs['model'] == 'GatedGCN':
        net = GatedGCN(len(src_w2idx), len(tgt_w2idx), kwargs['embed_size'], 
                    kwargs['utter_hidden'], kwargs['context_hidden'],
                    kwargs['decoder_hidden'], kwargs['position_embed_size'], 
                    teach_force=kwargs['teach_force'], pad=tgt_w2idx['<pad>'], 
                    sos=tgt_w2idx['<sos>'], dropout=kwargs['dropout'],
                    utter_n_layer=kwargs['utter_n_layer'],
                    context_threshold=kwargs['context_threshold'])
    else:
        raise Exception(f'[!] wrong model named {kwargs["model"]}')

    if torch.cuda.is_available():
        net.cuda()

    print('[!] Net:')
    print(net)

    print(f'[!] Parameters size: {sum(x.numel() for x in net.parameters())}')

    # prepare optimizer
    # if kwargs['model'] in ['ReCoSa', 'Transformer']:
    #    print(f'[!] Optimizer NoamOpt')
    #    optimizer = get_std_opt(net)
    # else:
    print(f'[!] Optimizer Adam')
    optimizer = optim.Adam(net.parameters(), lr=kwargs['lr'], 
                           weight_decay=kwargs['weight_decay'])

    pbar = tqdm(range(1, kwargs['epochs'] + 1))
    training_loss, validation_loss = [], []
    min_loss = np.inf
    patience = 0
    best_val_loss = None
    teacher_force_ratio = kwargs['teach_force']    # default 1
    teacher_force_ratio_counter = kwargs['dynamic_tfr_counter']
    holder = teacher_force_ratio_counter

    # train
    for epoch in pbar:
        # prepare dataset
        if kwargs['hierarchical'] == 1:
            if kwargs['graph'] == 1:
                train_iter = get_batch_data_graph(kwargs['src_train'], kwargs['tgt_train'], 
                                                  kwargs['train_graph'],
                                                  kwargs['src_vocab'], kwargs['tgt_vocab'],
                                                  kwargs['batch_size'], kwargs['maxlen'])
                test_iter = get_batch_data_graph(kwargs['src_test'], kwargs['tgt_test'], 
                                                 kwargs['test_graph'],
                                                 kwargs['src_vocab'], kwargs['tgt_vocab'],
                                                 kwargs['batch_size'], kwargs['maxlen'])
                dev_iter = get_batch_data_graph(kwargs['src_dev'], kwargs['tgt_dev'], 
                                                kwargs['dev_graph'],
                                                kwargs['src_vocab'], kwargs['tgt_vocab'],
                                                kwargs['batch_size'], kwargs['maxlen'])
            else:
                train_iter = get_batch_data(kwargs['src_train'], kwargs['tgt_train'],
                                            kwargs['src_vocab'], kwargs['tgt_vocab'], 
                                            kwargs['batch_size'], kwargs['maxlen'])
                test_iter = get_batch_data(kwargs['src_test'], kwargs['tgt_test'],
                                           kwargs['src_vocab'], kwargs['tgt_vocab'],
                                           kwargs['batch_size'], kwargs['maxlen'])
                dev_iter = get_batch_data(kwargs['src_dev'], kwargs['tgt_dev'],
                                          kwargs['src_vocab'], kwargs['tgt_vocab'],
                                          kwargs['batch_size'], kwargs['maxlen'])
        else:
            train_iter = get_batch_data_flatten(kwargs['src_train'], kwargs['tgt_train'],
                                                kwargs['src_vocab'], kwargs['tgt_vocab'],
                                                kwargs['batch_size'], kwargs['maxlen'])
            test_iter = get_batch_data_flatten(kwargs['src_test'], kwargs['tgt_test'],
                                               kwargs['src_vocab'], kwargs['tgt_vocab'],
                                               kwargs['batch_size'], kwargs['maxlen'])
            dev_iter = get_batch_data_flatten(kwargs['src_dev'], kwargs['tgt_dev'],
                                              kwargs['src_vocab'], kwargs['tgt_vocab'],
                                              kwargs['batch_size'], kwargs['maxlen'])

        writer_str = f'{kwargs["dataset"]}-{kwargs["model"]}'
        train(train_iter, net, optimizer, len(tgt_w2idx), tgt_w2idx['<pad>'], 
              grad_clip=kwargs['grad_clip'], debug=kwargs['debug'],
              transformer_decode=kwargs['transformer_decode'], graph=kwargs['graph']==1)
        val_loss = validation(dev_iter, net, len(tgt_w2idx), tgt_w2idx['<pad>'],
                              transformer_decode=kwargs['transformer_decode'],
                              graph=kwargs['graph']==1)
        # add scalar to tensorboard
        writer.add_scalar(f'{writer_str}-Loss/dev', val_loss, epoch)

        if not best_val_loss or val_loss < best_val_loss:
            best_val_loss = val_loss
            patience = 0
        else:
            patience += 1
                          
        state = {'net': net.state_dict(), 'epoch': epoch}
        torch.save(state, 
                       f'./ckpt/{kwargs["dataset"]}/{kwargs["model"]}/vloss_{val_loss}_epoch_{epoch}.pt')

        # if patience > kwargs['patience']:
        #     print(f'Early Stop {kwargs["patience"]} at epoch {epoch}')
        #     break
        
        # translate on test dataset
        ppl = translate(test_iter, net, **kwargs)
        
        # measure the performance, write into the tensorboard
        write_into_tb(kwargs['pred'], writer, writer_str, epoch, ppl)
        
        pbar.set_description(f'Epoch: {epoch}, val_loss: {val_loss}, val_ppl: {round(math.exp(val_loss), 4)}, patience: {patience}/{kwargs["patience"]}, tfr: {teacher_force_ratio}')
        
        # dynamic teach_force_ratio
        if epoch > kwargs["dynamic_tfr"]:
            if holder == 0:
                teacher_force_ratio /= kwargs["dynamic_tfr_weight"]
                holder = teacher_force_ratio_counter
            else:
                holder -= 1
            net.teach_force = teacher_force_ratio
        

    pbar.close()
    writer.close()



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Train script')
    parser.add_argument('--src_train', type=str, default=None, help='src train file')
    parser.add_argument('--tgt_train', type=str, default=None, help='src train file')
    parser.add_argument('--src_test', type=str, default=None, help='src test file')
    parser.add_argument('--tgt_test', type=str, default=None, help='tgt test file')
    parser.add_argument('--src_dev', type=str, default=None, help='src dev file')
    parser.add_argument('--tgt_dev', type=str, default=None, help='tgt dev file')
    parser.add_argument('--min_threshold', type=int, default=0, 
                        help='epoch threshold for loading best model')
    parser.add_argument('--max_threshold', type=int, default=20, 
                        help='epoch threshold for loading best model')
    parser.add_argument('--lr', type=float, default=1e-4, help='learning rate')
    parser.add_argument('--batch_size', type=int, default=16, help='batch size')
    parser.add_argument('--weight_decay', type=float, default=1e-6, help='weight decay')
    parser.add_argument('--model', type=str, default='HRED', help='model to be trained')
    parser.add_argument('--utter_hidden', type=int, default=150, 
                        help='utterance encoder hidden size')
    parser.add_argument('--teach_force', type=float, default=0.5, help='teach force ratio')
    parser.add_argument('--context_hidden', type=int, default=150, 
                        help='context encoder hidden size')
    parser.add_argument('--decoder_hidden', type=int, default=150, 
                        help='decoder hidden size')
    parser.add_argument('--seed', type=int, default=30,
                        help='random seed')
    parser.add_argument('--embed_size', type=int, default=200, 
                        help='embedding layer size')
    parser.add_argument('--patience', type=int, default=5, help='patience for early stop')
    parser.add_argument('--dataset', type=str, default='dailydialog', 
                        help='dataset for training')
    parser.add_argument('--grad_clip', type=float, default=10.0, help='grad clip')
    parser.add_argument('--epochs', type=int, default=20, help='epochs for training')
    parser.add_argument('--src_vocab', type=str, default=None, help='src vocabulary')
    parser.add_argument('--tgt_vocab', type=str, default=None, help='tgt vocabulary')
    parser.add_argument('--maxlen', type=int, default=50, help='the maxlen of the utterance')
    parser.add_argument('--utter_n_layer', type=int, default=1, 
                        help='layers of the utterance encoder')
    parser.add_argument('--debug', dest='debug', action='store_true')
    parser.add_argument('--no-debug', dest='debug', action='store_false')
    parser.add_argument('--dropout', type=float, default=0.5, help='dropout ratio')
    parser.add_argument('--hierarchical', type=int, default=1, help='Whether hierarchical architecture')
    parser.add_argument('--transformer_decode', type=int, default=0,
                        help='transformer decoder need a little different training process')
    parser.add_argument('--d_model', type=int, default=512, help='d_model for transformer')
    parser.add_argument('--pretrained', type=str, default='', 
                        help='whether use the pretrained embedding')
    parser.add_argument('--contextrnn', dest='contextrnn', action='store_true')
    parser.add_argument('--no-contextrnn', dest='contextrnn', action='store_false')
    parser.add_argument('--position_embed_size', type=int, default=30)
    parser.add_argument('--graph', type=int, default=0)
    parser.add_argument('--train_graph', type=str, default=None, help='train graph data path')
    parser.add_argument('--test_graph', type=str, default=None, help='test graph data path')
    parser.add_argument('--dev_graph', type=str, default=None, help='dev graph data path')
    parser.add_argument('--context_threshold', type=int, default=3, help='low turns filter')
    parser.add_argument('--pred', type=str, default=None, help='the file save the output')
    parser.add_argument('--dynamic_tfr', type=int, default=20, help='begin to use the dynamic teacher forcing ratio, each ratio divide the tfr_weight')
    parser.add_argument('--dynamic_tfr_weight', type=float, default=2)
    parser.add_argument('--dynamic_tfr_counter', type=int, default=5)


    args = parser.parse_args()

    # show the parameters
    print('[!] Parameters:')
    print(args)

    # set random seed
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(args.seed)

    # main function
    args_dict = vars(args)
    main(**args_dict)