import math

import torch
import torch.nn as nn

from models.neural import MultiHeadedAttention, PositionwiseFeedForward
from models.rnn import LayerNormLSTM


class Classifier(nn.Module):
    def __init__(self, hidden_size):
        super(Classifier, self).__init__()
        self.linear1 = nn.Linear(hidden_size, 1)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x, mask_cls):
        h = self.linear1(x).squeeze(-1)
        sent_scores = self.sigmoid(h) * mask_cls.float()
        return sent_scores


class PositionalEncoding(nn.Module):

    def __init__(self, dropout, dim, max_len=5000):
        pe = torch.zeros(max_len, dim)
        position = torch.arange(0, max_len).unsqueeze(1)
        div_term = torch.exp((torch.arange(0, dim, 2, dtype=torch.float) *
                              -(math.log(10000.0) / dim)))
        pe[:, 0::2] = torch.sin(position.float() * div_term)
        pe[:, 1::2] = torch.cos(position.float() * div_term)
        pe = pe.unsqueeze(0)
        super(PositionalEncoding, self).__init__()
        self.register_buffer('pe', pe)
        self.dropout = nn.Dropout(p=dropout)
        self.dim = dim

    def forward(self, emb, step=None):
        emb = emb * math.sqrt(self.dim)
        if (step):
            emb = emb + self.pe[:, step][:, None, :]

        else:
            emb = emb + self.pe[:, :emb.size(1)]
        emb = self.dropout(emb)
        return emb

    def get_emb(self, emb):
        return self.pe[:, :emb.size(1)]


class TransformerEncoderLayer(nn.Module):
    def __init__(self, d_model, heads, d_ff, dropout):
        super(TransformerEncoderLayer, self).__init__()

        self.self_attn = MultiHeadedAttention(
            heads, d_model, dropout=dropout)
        self.feed_forward = PositionwiseFeedForward(d_model, d_ff, dropout)
        self.layer_norm = nn.LayerNorm(d_model, eps=1e-6)
        self.dropout = nn.Dropout(dropout)

    def forward(self, iter, query, inputs, mask):
        if (iter != 0):
            input_norm = self.layer_norm(inputs)
        else:
            input_norm = inputs

        mask = mask.unsqueeze(1)
        context = self.self_attn(input_norm, input_norm, input_norm,
                                 mask=mask)
        out = self.dropout(context) + inputs
        return self.feed_forward(out)

class TransformerInterEncoderClassifier(nn.Module):
    def __init__(self, d_model, d_ff, heads, dropout, num_inter_layers=0):
        super(TransformerInterEncoderClassifier, self).__init__()
        self.d_model = d_model
        self.num_inter_layers = num_inter_layers
        self.pos_emb = PositionalEncoding(dropout, d_model)
        self.transformer_inter = nn.ModuleList(
            [TransformerEncoderLayer(d_model, heads, d_ff, dropout)
             for _ in range(num_inter_layers)])
        self.dropout = nn.Dropout(dropout)
        self.layer_norm = nn.LayerNorm(d_model, eps=1e-6)
        self.wo = nn.Linear(d_model, 1, bias=True)
        self.sigmoid = nn.Sigmoid()

    def forward(self, top_vecs, mask):
        """ See :obj:`EncoderBase.forward()`"""

        batch_size, n_sents = top_vecs.size(0), top_vecs.size(1)
        x = top_vecs * mask[:, :, None].float()

        pos_emb = self.pos_emb.pe[:, :n_sents]
        x = x + pos_emb

        for i in range(self.num_inter_layers):
            x = self.transformer_inter[i](i, x, x, 1 - mask)  # all_sents * max_tokens * dim

        x = self.layer_norm(x)
        sent_scores = self.sigmoid(self.wo(x))
        sent_scores = sent_scores.squeeze(-1) * mask.float()

        return sent_scores


class TransformerInterEncoder(nn.Module):
    def __init__(self, d_model, d_ff, heads, dropout, num_inter_layers=0):
        super(TransformerInterEncoder, self).__init__()
        self.d_model = d_model
        self.num_inter_layers = num_inter_layers
        self.pos_emb = PositionalEncoding(dropout, d_model)
        self.doc_emb = torch.nn.Parameter(torch.rand(1, d_model))
        self.transformer_inter = nn.ModuleList(
            [TransformerEncoderLayer(d_model, heads, d_ff, dropout)
             for _ in range(num_inter_layers)])
        self.layer_norm = nn.LayerNorm(d_model, eps=1e-6)

    def forward(self, top_vecs, mask):
        """ See :obj:`EncoderBase.forward()`"""

        batch_size, n_sents = top_vecs.size(0), top_vecs.size(1)
        #batch_size, n_sents,
        x = top_vecs * mask[:, :, None].float()

        pad_mask = (torch.ones(batch_size, 1)==1).to(mask.device)
        mask = torch.cat([pad_mask, mask], dim=1)

        pos_emb = self.pos_emb.pe[:, :n_sents+1]
        x = torch.cat([self.doc_emb.unsqueeze(0).expand(batch_size, -1, -1), x], dim=1)
        #(batch_size, 1, d_model) concat (batch_size, n_sents, d_model)
        x = x + pos_emb

        for i in range(self.num_inter_layers):
            x = self.transformer_inter[i](i, x, x, 1 - mask)  # all_sents * max_tokens * dim

        x = self.layer_norm(x)
        x = x * mask[:, :, None].float()
        doc_emb = x[:,:1,:]# x[:,0,:] will return size batch_size, d_model
        sents_emb = x[:,1:,:]
        #print("doc_emb", doc_emb.size())
        #print("sents_emb", sents_emb.size())
        return doc_emb, sents_emb


class RNNEncoder(nn.Module):

    def __init__(self, bidirectional, num_layers, input_size,
                 hidden_size, dropout=0.0):
        super(RNNEncoder, self).__init__()
        num_directions = 2 if bidirectional else 1
        assert hidden_size % num_directions == 0
        hidden_size = hidden_size // num_directions

        self.rnn = LayerNormLSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            bidirectional=bidirectional)

        self.wo = nn.Linear(num_directions * hidden_size, 1, bias=True)
        self.dropout = nn.Dropout(dropout)
        self.sigmoid = nn.Sigmoid()

    def forward(self, x, mask):
        """See :func:`EncoderBase.forward()`"""
        x = torch.transpose(x, 1, 0)
        memory_bank, _ = self.rnn(x)
        memory_bank = self.dropout(memory_bank) + x
        memory_bank = torch.transpose(memory_bank, 1, 0)

        sent_scores = self.sigmoid(self.wo(memory_bank))
        sent_scores = sent_scores.squeeze(-1) * mask.float()
        return sent_scores
