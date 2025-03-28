
import os
#os.environ["CUDA_LAUNCH_BLOCKING"] = "1"
import sys
import copy
import math
import torch
import random
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
from torch_scatter import scatter_add
from torch_geometric.utils import softmax, dense_to_sparse, degree
from torch_geometric.data import Data
from torch_geometric.nn import GlobalAttention
from torch_geometric.nn import SAGEConv,LayerNorm,PNAConv
from mae_utils import get_sinusoid_encoding_table,Block
from timm.models.layers import trunc_normal_ as __call_trunc_normal_
device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')


def reset(nn):
    def _reset(item):
        if hasattr(item, 'reset_parameters'):
            item.reset_parameters()

    if nn is not None:
        if hasattr(nn, 'children') and len(list(nn.children())) > 0:
            for item in nn.children():
                _reset(item)
        else:
            _reset(nn)

class my_GlobalAttention(torch.nn.Module):
    def __init__(self, gate_nn, nn=None):
        super(my_GlobalAttention, self).__init__()
        self.gate_nn = gate_nn
        self.nn = nn

        self.reset_parameters()

    def reset_parameters(self):
        reset(self.gate_nn)
        reset(self.nn)


    def forward(self, x, batch, size=None):
        """"""
        x = x.unsqueeze(-1) if x.dim() == 1 else x
        size = batch[-1].item() + 1 if size is None else size
        
        gate = self.gate_nn(x).view(-1, 1)
        x = self.nn(x) if self.nn is not None else x
        assert gate.dim() == x.dim() and gate.size(0) == x.size(0)

        gate = softmax(gate, batch, num_nodes=size)
        out = scatter_add(gate * x, batch, dim=0, dim_size=size)

        return out,gate


    def __repr__(self):
        return '{}(gate_nn={}, nn={})'.format(self.__class__.__name__,
                                              self.gate_nn, self.nn)
    
    
def trunc_normal_(tensor, mean=0., std=1.):
    __call_trunc_normal_(tensor, mean=mean, std=std, a=-std, b=std)
    
class PretrainVisionTransformerEncoder(nn.Module):
    """ Vision Transformer with support for patch or hybrid CNN input stage
    """
    def __init__(self, img_size=224, patch_size=16, in_chans=3, num_classes=0, embed_dim=512, depth=12,
                 num_heads=12, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0., norm_layer=nn.LayerNorm, init_values=None,
                 use_learnable_pos_emb=False,train_type_num=5):
        super().__init__()
        self.num_classes = num_classes
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models

#         self.patch_embed = PatchEmbed(
#             img_size=img_size, patch_size=patch_size, in_chans=in_chans, embed_dim=embed_dim)
#         num_patches = self.patch_embed.num_patches

        self.patch_embed = nn.Linear(embed_dim,embed_dim)
        num_patches = train_type_num

        # TODO: Add the cls token
        # self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        if use_learnable_pos_emb:
            self.pos_embed = nn.Parameter(torch.zeros(1, num_patches + 1, embed_dim))
        else:
            # sine-cosine positional embeddings 
            self.pos_embed = get_sinusoid_encoding_table(num_patches, embed_dim)

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer,
                init_values=init_values)
            for i in range(depth)])
        self.norm =  norm_layer(embed_dim)
        self.head = nn.Linear(embed_dim, num_classes) if num_classes > 0 else nn.Identity()

        if use_learnable_pos_emb:
            trunc_normal_(self.pos_embed, std=.02)

        # trunc_normal_(self.cls_token, std=.02)
        self.apply(self._init_weights)


    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def get_num_layers(self):
        return len(self.blocks)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token'}

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def forward_features(self, x, mask):
        x = self.patch_embed(x)
        
        # cls_tokens = self.cls_token.expand(batch_size, -1, -1) 
        # x = torch.cat((cls_tokens, x), dim=1)
        x = x + self.pos_embed.type_as(x).to(x.device).clone().detach()

        B, _, C = x.shape
        # ~ make true to false or else.
        # masked a path in [img,cli,rna]
        # print(x.shape)
        x_vis = x[~mask].reshape(B, -1, C) # ~mask means visible

        for blk in self.blocks:
            x_vis = blk(x_vis)

        x_vis = self.norm(x_vis)
        return x_vis

    def forward(self, x, mask):
        x = self.forward_features(x, mask)
        x = self.head(x)
        return x

class PretrainVisionTransformerDecoder(nn.Module):
    """ Vision Transformer with support for patch or hybrid CNN input stage
    """
    def __init__(self, patch_size=16, num_classes=512, embed_dim=512, depth=12,
                 num_heads=12, mlp_ratio=4., qkv_bias=False, qk_scale=None, drop_rate=0., attn_drop_rate=0.,
                 drop_path_rate=0., norm_layer=nn.LayerNorm, init_values=None, num_patches=196,train_type_num=5,
                 ):
        super().__init__()
        self.num_classes = num_classes
#         assert num_classes == 3 * patch_size ** 2
        self.num_features = self.embed_dim = embed_dim  # num_features for consistency with other models
#         self.patch_size = patch_size

        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]  # stochastic depth decay rule
        self.blocks = nn.ModuleList([
            Block(
                dim=embed_dim, num_heads=num_heads, mlp_ratio=mlp_ratio, qkv_bias=qkv_bias, qk_scale=qk_scale,
                drop=drop_rate, attn_drop=attn_drop_rate, drop_path=dpr[i], norm_layer=norm_layer,
                init_values=init_values)
            for i in range(depth)])
        self.norm =  norm_layer(embed_dim)
        self.head = nn.Linear(embed_dim, num_classes) if num_classes > 0 else nn.Identity()

        self.apply(self._init_weights)


    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def get_num_layers(self):
        return len(self.blocks)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token'}

    def get_classifier(self):
        return self.head

    def reset_classifier(self, num_classes, global_pool=''):
        self.num_classes = num_classes
        self.head = nn.Linear(self.embed_dim, num_classes) if num_classes > 0 else nn.Identity()

    def forward(self, x, return_token_num):
        for blk in self.blocks:
            x = blk(x)


        if return_token_num > 0:
            x = self.head(self.norm(x[:, -return_token_num:])) # only return the mask tokens predict pixels
        else:
            x = self.head(self.norm(x)) # [B, N, 3*16^2]

        return x

class PretrainVisionTransformer(nn.Module):
    """ Vision Transformer with support for patch or hybrid CNN input stage
    """
    def __init__(self,
                 img_size=224, 
                 patch_size=16, 
                 encoder_in_chans=5,
                 encoder_num_classes=0, 
                 encoder_embed_dim=512, 
                 encoder_depth=12,
                 encoder_num_heads=12, 
                 decoder_num_classes=512, 
                 decoder_embed_dim=512, 
                 decoder_depth=8,
                 decoder_num_heads=8, 
                 mlp_ratio=4., 
                 qkv_bias=False, 
                 qk_scale=None, 
                 drop_rate=0., 
                 attn_drop_rate=0.3,
                 drop_path_rate=0.3, 
                 norm_layer=nn.LayerNorm, 
                 init_values=0.,
                 use_learnable_pos_emb=False,
                 num_classes=0, # avoid the error from create_fn in timm
                 in_chans=0, # avoid the error from create_fn in timm
                 train_type_num=5,
                 ):
        super().__init__()
        self.encoder = PretrainVisionTransformerEncoder(
            img_size=img_size, 
            patch_size=patch_size, 
            in_chans=encoder_in_chans, 
            num_classes=encoder_num_classes, 
            embed_dim=encoder_embed_dim, 
            depth=encoder_depth,
            num_heads=encoder_num_heads, 
            mlp_ratio=mlp_ratio, 
            qkv_bias=qkv_bias, 
            qk_scale=qk_scale, 
            drop_rate=drop_rate, 
            attn_drop_rate=attn_drop_rate,
            drop_path_rate=drop_path_rate, 
            norm_layer=norm_layer, 
            init_values=init_values,
            use_learnable_pos_emb=use_learnable_pos_emb,
            train_type_num=train_type_num)

        self.decoder = PretrainVisionTransformerDecoder(
            patch_size=patch_size, 
            num_patches=3,
            num_classes=decoder_num_classes, 
            embed_dim=decoder_embed_dim, 
            depth=decoder_depth,
            num_heads=decoder_num_heads, 
            mlp_ratio=mlp_ratio, 
            qkv_bias=qkv_bias, 
            qk_scale=qk_scale, 
            drop_rate=drop_rate, 
            attn_drop_rate=attn_drop_rate,
            drop_path_rate=drop_path_rate, 
            norm_layer=norm_layer, 
            init_values=init_values,
            train_type_num=train_type_num)

        self.encoder_to_decoder = nn.Linear(encoder_embed_dim, decoder_embed_dim, bias=False)

        self.mask_token = nn.Parameter(torch.zeros(1, 1, decoder_embed_dim))
#         self.mask_token = torch.zeros(1, 1, decoder_embed_dim).to(device)
        

        self.pos_embed = get_sinusoid_encoding_table(train_type_num, decoder_embed_dim)

        trunc_normal_(self.mask_token, std=.02)


    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            nn.init.xavier_uniform_(m.weight)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def get_num_layers(self):
        return len(self.blocks)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token', 'mask_token'}

    def forward(self, x, mask):
        # encoder and decoder 计算顺序：
        # mask
        # block
        #   attention(get qkv, attn = q*k, output = attn*v)
        #   mlp
        # head(linear)
        x_vis = self.encoder(x, mask) # [B, N_vis, C_e]
        x_vis = self.encoder_to_decoder(x_vis) # [B, N_vis, C_d]

        B, N, C = x_vis.shape
        
        # we don't unshuffle the correct visible token order, 
        # but shuffle the pos embedding accorddingly.
        expand_pos_embed = self.pos_embed.expand(B, -1, -1).type_as(x).to(x.device).clone().detach()
        pos_emd_vis = expand_pos_embed[~mask].reshape(B, -1, C)
        pos_emd_mask = expand_pos_embed[mask].reshape(B, -1, C)
        x_full = torch.cat([x_vis + pos_emd_vis, self.mask_token + pos_emd_mask], dim=1)

        # notice: if N_mask==0, the shape of x is [B, N_mask, 3 * 16 * 16]
        x = self.decoder(x_full, 0) # [B, N_mask, 3 * 16 * 16]

        tmp_x = torch.zeros_like(x).to(device)
        Mask_n = 0
        Truth_n = 0
        for i,flag in enumerate(mask[0][0]):
            if flag:  
                tmp_x[:,i] = x[:,pos_emd_vis.shape[1]+Mask_n]
                Mask_n += 1
            else:
                tmp_x[:,i] = x[:,Truth_n]
                Truth_n += 1
        return tmp_x



def Mix_mlp(dim1):
    
    return nn.Sequential(
            nn.Linear(dim1, dim1),
            nn.GELU(),
            nn.Linear(dim1, dim1))

class MixerBlock(nn.Module):
    def __init__(self,dim1,dim2):
        super(MixerBlock,self).__init__() 
        
        self.norm = LayerNorm(dim1)
        self.mix_mip_1 = Mix_mlp(dim1)
        self.mix_mip_2 = Mix_mlp(dim2)
        
    def forward(self,x): 
        
        y = self.norm(x)
        y = y.transpose(0,1)
        y = self.mix_mip_1(y)
        y = y.transpose(0,1)
        x = x + y
        y = self.norm(x)
        x = x + self.mix_mip_2(y)
        
#         y = self.norm(x)
#         y = y.transpose(0,1)
#         y = self.mix_mip_1(y)
#         y = y.transpose(0,1)
#         x = self.norm(y)
        return x



def MLP_Block(dim1, dim2, dropout=0.3):
    r"""
    Multilayer Reception Block w/ Self-Normalization (Linear + ELU + Alpha Dropout)
    args:
        dim1 (int): Dimension of input features
        dim2 (int): Dimension of output features
        dropout (float): Dropout rate
    """
    return nn.Sequential(
            nn.Linear(dim1, dim2),
            nn.ReLU(),
            nn.Dropout(p=dropout))

def GNN_relu_Block(dim2, dropout=0.3):
    r"""
    Multilayer Reception Block w/ Self-Normalization (Linear + ELU + Alpha Dropout)
    args:
        dim1 (int): Dimension of input features
        dim2 (int): Dimension of output features
        dropout (float): Dropout rate
    """
    return nn.Sequential(
#             GATConv(in_channels=dim1,out_channels=dim2),
            nn.ReLU(),
            LayerNorm(dim2),
            nn.Dropout(p=dropout))

class merge_attention(nn.Module):
    def __init__(self, dim, merge_factor=2):
        super(merge_attention, self).__init__()
        self.q_linear = nn.Sequential(nn.Linear(dim, dim//2), nn.ReLU(), nn.Linear(dim//2, dim//4))
        self.k_linear = nn.Sequential(nn.Linear(dim, dim//2), nn.ReLU(), nn.Linear(dim//2, dim//4))
        
        self.high_reduce_dim = nn.Sequential(nn.Linear(dim, dim//4), nn.ReLU(), nn.Linear(dim//4, dim//8))
        #self.high_merge_linear = nn.Bilinear(dim//8, dim//8, dim)
        #self.high_xa_linear = nn.Linear(dim, dim//4)
        self.high_linear = nn.Linear(dim//8, dim)
        
        self.out_linear = nn.Sequential(nn.Linear(dim, dim//4), nn.ReLU(), nn.Linear(dim//4, dim),nn.Dropout(0.4))
        
        self.norm = LayerNorm(dim)

        self.merge_factor = merge_factor
        self.embed_dim = dim

    def forward(self, x):
        q = self.q_linear(x)
        k = self.k_linear(x)
        # 注意力计算
        attn = torch.matmul(q,k.transpose(-2,-1))
        scale_factor = self.embed_dim ** 0.5
        attn = attn / scale_factor
        attn = F.softmax(attn,dim=-1)
        # attn = F.softmax(attn,dim=1)
        # 特征值排序
        attn_scores = torch.max(attn, dim=-1).values
        #attn_scores = torch.sum(attn, dim=-1)
        sorted_attn, sorted_indices = torch.sort(attn_scores, descending=True)
        #sorted_attn = F.softmax(sorted_attn, dim=1)
        #print(sorted_attn)
        sorted_attn = attn[sorted_indices]
        sorted_x = x[sorted_indices]
        #print(sorted_attn.shape, sorted_x.shape)
        sorted_x = sorted_attn.transpose(0,1) @ sorted_x + sorted_x
        # 将有序特征值切分成前一半（偶数条）和后一半奇数条
        size = x.shape[0]
        high_size = (size // self.merge_factor) // 2
        high_size = high_size * self.merge_factor
        high_x = sorted_x[:high_size,:]
        low_x = sorted_x[high_size:,:]
        
        high_x = self.high_reduce_dim(high_x)
        high_x = high_x.reshape([-1,self.merge_factor,self.embed_dim//8])
        high_x = torch.sum(high_x,dim=1)
        high_x = self.high_linear(high_x)
        #high_xa = self.high_xa_linear(high_xa)
        #high_xb = self.high_merge_linear(high_x[:,0,:],high_x[:,1,:])
        #high_x = high_xa + high_xb
        #high_x = self.high_merge_linear(high_x[:,0,:],high_x[:,1,:])
        #high_x = self.high_linear(high_x)

        low_x = torch.sum(low_x,dim=0).unsqueeze(0)
        
        out = torch.cat((high_x,low_x),dim=0)
        out =self.out_linear(out) + out
        out = self.norm(out)
        
        return out

class dynamic_graph(nn.Module):
    def __init__(self,dim,is_filted = True,std_factor=.2,k_weight=.3,filter_factor=0.4):
        super(dynamic_graph, self).__init__()
        self.q_linear = nn.Sequential(nn.Linear(dim, dim//2), nn.ReLU(), nn.Linear(dim//2, dim//4))
        self.k_linear = nn.Sequential(nn.Linear(dim, dim//2), nn.ReLU(), nn.Linear(dim//2, dim//4))

        self.q_linear2 = nn.Sequential(nn.Linear(dim//4, dim//8), nn.ReLU(), nn.Linear(dim//8, dim//8))
        self.k_linear2 = nn.Sequential(nn.Linear(dim//4, dim//8), nn.ReLU(), nn.Linear(dim//8, dim//8))
        #self.v_linear2 = nn.Sequential(nn.Linear(dim//4, dim//8), nn.ReLU(), nn.Linear(dim//8, dim//8))
        
        self.out_linear = nn.Sequential(nn.Linear(dim//4, dim//2), nn.ReLU(), nn.Linear(dim//2, dim),nn.Dropout(0.4))
        self.norm = LayerNorm(dim)

        self.is_filted = is_filted
        self.filter_factor = filter_factor
        self.std_factor = std_factor
        self.k_weight = k_weight
        self.dim = dim
        self.dropout = nn.Dropout(p=.4)

    def forward(self,q,k):
        q = self.q_linear(q)
        k = self.k_linear(k)

        # 适配性筛选
        if self.is_filted:
            attn = torch.matmul(q,k.transpose(-2,-1))
            attn = attn/(self.dim**.5)
            attn = attn.softmax(dim=-1)
            attn = torch.max(attn,dim=0).values
            _ , sorted_indices = torch.sort(attn, descending=True)
            index_edge = int(sorted_indices.shape[0] * self.filter_factor) + 1
            index = sorted_indices[:index_edge]

            #构图
            # 计算注意力值
            # 根据注意力构建邻接矩阵
            # 稀疏化存储
            k = k[index]
            k = self.k_weight * k
            node = torch.cat((q,k),dim=0)
        else:
            node = q
        q2=self.q_linear2(node)
        k2=self.k_linear2(node)
        #node = self.v_linear2(node)
        attn2 = torch.matmul(q2,k2.transpose(-2,-1))
        attn2 = attn2/(self.dim**.5)
        attn2 = attn2.softmax(dim=0)
        node = torch.matmul(attn2,node) + node
        node = self.out_linear(node)
        node = self.norm(node)
        
        
        thresold = attn2.mean() + self.std_factor * attn2.std()
        adj_matrix = torch.where(attn2 > thresold, 1, 0)
        
        # adj_matrix = attn2[1 if attn2>thresold else 0]
        (edge,edge_weights) = dense_to_sparse(adj_matrix)
        edge = edge.long()
        edge_weights = edge_weights.unsqueeze(-1)
        return node,edge,edge_weights




class fusion_model_mae_2(nn.Module):
    def __init__(self,in_feats,n_hidden,out_classes,
                 k_weight_rna=1., k_weight_cli=1.,
                 img_std_factor=.2,
                 rna_std_factor=.2,
                 cli_std_factor=.2,
                 dropout=0.3,train_type_num=5,):
        super(fusion_model_mae_2,self).__init__() 

        self.merge_attention = merge_attention(in_feats,merge_factor=4)
        self.merge_linear = nn.Linear(in_feats,in_feats)
        self.merge_loss_linear = nn.Linear(in_feats,out_classes)

        self.k_weight_rna = k_weight_rna
        self.k_weight_cli = k_weight_cli
        self.img_std_factor = nn.Parameter(torch.Tensor([img_std_factor,]))
        self.rna_std_factor = nn.Parameter(torch.Tensor([rna_std_factor,]))
        self.cli_std_factor = nn.Parameter(torch.Tensor([cli_std_factor,]))
        self.img_dynamic_graph = dynamic_graph(in_feats,is_filted=False,std_factor=self.img_std_factor)
        self.cli_dynamic_graph = dynamic_graph(in_feats,is_filted=True,std_factor=self.cli_std_factor,k_weight=self.k_weight_cli)
        self.rna_dynamic_graph = dynamic_graph(in_feats,is_filted=True,std_factor=self.rna_std_factor,k_weight=self.k_weight_rna)
        
        # graph conv(GraphSAGE conv)
        self.img_gnn_2 = SAGEConv(in_channels=in_feats,out_channels=out_classes)
        self.img_relu_2 = GNN_relu_Block(out_classes)
        
        self.imgb_gnn_2_linear = nn.Linear(out_classes, in_feats)
        self.imgc_gnn_2_linear = nn.Linear(out_classes, in_feats)
        
        self.imgb_gnn_2 = SAGEConv(in_channels=in_feats,out_channels=out_classes)
        self.imgb_relu_2 = GNN_relu_Block(out_classes)
        
        self.imgc_gnn_2 = SAGEConv(in_channels=in_feats,out_channels=out_classes)
        self.imgc_relu_2 = GNN_relu_Block(out_classes)
        
        self.rna_gnn_2 = SAGEConv(in_channels=in_feats,out_channels=out_classes)
        self.rna_relu_2 = GNN_relu_Block(out_classes)
        
        self.cli_gnn_2 = SAGEConv(in_channels=in_feats,out_channels=out_classes)
        self.cli_relu_2 = GNN_relu_Block(out_classes)

	# mpool
        att_net_img = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, 1))
        self.mpool_img = my_GlobalAttention(att_net_img)

        att_net_img_b = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, 1))
        self.mpool_img_b = my_GlobalAttention(att_net_img_b)

        att_net_img_c = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, 1))
        self.mpool_img_c = my_GlobalAttention(att_net_img_c)


        att_net_rna = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, 1))
        self.mpool_rna = my_GlobalAttention(att_net_rna)

        att_net_cli = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, 1))
        self.mpool_cli = my_GlobalAttention(att_net_cli)

        # pool 2
        '''
        self.img_res_linear = nn.Linear(in_feats, out_classes)
        self.imgb_res_linear = nn.Linear(in_feats, out_classes)
        self.imgc_res_linear = nn.Linear(in_feats, out_classes)
        self.rna_res_linear = nn.Linear(in_feats, out_classes)
        self.cli_res_linear = nn.Linear(in_feats, out_classes)
        self.img_res_linear = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, out_classes))
        self.imgb_res_linear = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, out_classes))
        self.imgc_res_linear = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, out_classes))
        self.rna_res_linear = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, out_classes))
        self.cli_res_linear = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, out_classes))
        '''
        
        att_net_img_2 = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, 1))
        self.mpool_img_2 = my_GlobalAttention(att_net_img_2)
        att_net_img_2_b = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, 1))
        self.mpool_img_2_b = my_GlobalAttention(att_net_img_2_b)
        att_net_img_2_c = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, 1))
        self.mpool_img_2_c = my_GlobalAttention(att_net_img_2_c)

        att_net_rna_2 = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, 1))        
        self.mpool_rna_2 = my_GlobalAttention(att_net_rna_2)        

        att_net_cli_2 = nn.Sequential(nn.Linear(out_classes, out_classes//4), nn.ReLU(), nn.Linear(out_classes//4, 1))
        self.mpool_cli_2 = my_GlobalAttention(att_net_cli_2)
        
        
        # transformer and mix block
        self.mae = PretrainVisionTransformer(encoder_embed_dim=out_classes, decoder_num_classes=out_classes, decoder_embed_dim=out_classes, encoder_depth=1,decoder_depth=1,train_type_num=train_type_num)
        # self.mix = MixerBlock(train_type_num, out_classes)
        self.mix = nn.Sequential(nn.Linear(out_classes, out_classes), nn.ReLU(), nn.Linear(out_classes, out_classes))

        self.lin1_img = torch.nn.Linear(out_classes,out_classes//4)
        self.lin2_img = torch.nn.Linear(out_classes//4,1)     
        self.lin1_imgb = torch.nn.Linear(out_classes,out_classes//4)
        self.lin2_imgb = torch.nn.Linear(out_classes//4,1)  
        self.lin1_imgc = torch.nn.Linear(out_classes,out_classes//4)
        self.lin2_imgc = torch.nn.Linear(out_classes//4,1)     
        self.lin1_rna = torch.nn.Linear(out_classes,out_classes//4)
        self.lin2_rna = torch.nn.Linear(out_classes//4,1) 
        self.lin1_cli = torch.nn.Linear(out_classes,out_classes//4)
        self.lin2_cli = torch.nn.Linear(out_classes//4,1)

        self.norm_img = LayerNorm(out_classes//4)
        self.norm_imgb = LayerNorm(out_classes//4)
        self.norm_imgc = LayerNorm(out_classes//4)
        self.norm_rna = LayerNorm(out_classes//4)
        self.norm_cli = LayerNorm(out_classes//4)
        self.relu = torch.nn.ReLU() 
        self.dropout=nn.Dropout(p=dropout)
        
        #self.img_rate = nn.Parameter(torch.Tensor([[1.0,],[1.0,],[1.0,],]))
        #self.rna_rate = nn.Parameter(torch.Tensor([1.0,]))
        #self.cli_rate = nn.Parameter(torch.Tensor([1.0,]))



    def forward(self,all_thing,train_use_type=None,use_type=None,in_mask=[],mix=False):
        # get mask type
        mask = in_mask
        if 'img' in train_use_type:
            train_use_type = ['img', 'imgb', 'imgc'] + train_use_type[1:]
        if 'img' in use_type:
            use_type = ['img', 'imgb', 'imgc'] + use_type[1:]
        data_type = use_type
        if len(in_mask) == 0:
            mask = np.array([[[False]*len(train_use_type)]])
        else:
            if 'img' in use_type:
                mask = np.append((in_mask[0][0][0],in_mask[0][0][0],in_mask[0][0][0]),in_mask[0][0][1:]).reshape([1,1,5])

        # the input data features
        x_img = all_thing.x_img
        x_rna = all_thing.x_rna
        x_cli = all_thing.x_cli

        data_id=all_thing.data_id
        edge_index_img=all_thing.edge_index_image
        edge_index_rna=all_thing.edge_index_rna
        edge_index_cli=all_thing.edge_index_cli

        save_fea = {}
        fea_dict = {}
        # num_img = len(x_img)
        # num_rna = len(x_rna)
        # num_cli = len(x_cli)
        x_img_rna = None
        x_img_cli = None
        # merge and dynamic graph net once
        if 'img' in data_type:
            x_img = self.merge_attention(x_img)
            x_img = self.merge_linear(x_img)
            # for merge loss
            if x_img.shape[0] >= 10:
                loss_img = x_img[:10,:]
            else:
                loss_img = x_img
            #loss_img = torch.sum(loss_img, dim=1)
            loss_img = self.merge_loss_linear(loss_img)
            loss_img = self.lin1_img(loss_img)
            loss_img = self.relu(loss_img)
            loss_img = self.norm_img(loss_img)
            loss_img = self.dropout(loss_img)

            loss_img = self.lin2_img(loss_img)
            fea_dict['loss_img'] = loss_img

            _, edge_index_img, edge_weights_img = self.img_dynamic_graph(x_img,x_img)
            if 'cli' in data_type:
                x_img_cli,edge_index_img_cli, edge_weights_img_cli = self.cli_dynamic_graph(x_cli,x_img)

            if 'rna' in data_type:
                x_img_rna,edge_index_img_rna, edge_weights_img_rna = self.rna_dynamic_graph(x_rna,x_img)
        att_2 = []
        # graph net
        # make per model features cat to pool_x final shap is (3,512)
        # the pool_x is a temporary container to save the feature of every model in this calculator block
        pool_x = torch.empty((0)).to(device)
        o_x_img = x_img
        o_x_rna = x_rna
        o_x_cli = x_cli


        if 'img' in data_type:
            #print(x_img.shape)
            x_img = self.img_gnn_2(x_img,edge_index_img)
            x_img = self.img_relu_2(x_img)
            
            #print(x_img.shape)
            batch = torch.zeros(len(x_img),dtype=torch.long).to(device)
            pool_x_img,att_img_2 = self.mpool_img(x_img,batch)
            att_2.append(att_img_2)
            pool_x = torch.cat((pool_x,pool_x_img),0)
        if 'imgb' in data_type:
            if not x_img_rna==None:
                x_imgb = self.imgb_gnn_2(x_img_rna,edge_index_img_rna)
                x_imgb = self.imgb_relu_2(x_imgb)
            else:
                # print(edge_index_img)
                x_imgb = self.imgb_gnn_2_linear(x_img)
                x_imgb = x_imgb + o_x_img
            
                x_imgb = self.imgb_gnn_2(x_imgb,edge_index_img)
                x_imgb = self.imgb_relu_2(x_imgb)
            
            batch = torch.zeros(len(x_imgb),dtype=torch.long).to(device)
            pool_x_img_b,att_img_2b = self.mpool_img_b(x_imgb,batch)
            att_2.append(att_img_2b)
            pool_x = torch.cat((pool_x,pool_x_img_b),0)
        if 'imgc' in data_type:
            if not x_img_cli==None:
                x_imgc = self.imgc_gnn_2(x_img_cli, edge_index_img_cli)
                x_imgc = self.imgc_relu_2(x_imgc)
            else:
                # print(edge_index_img)
                x_imgc = self.imgc_gnn_2_linear(x_img)
                x_imgc = x_imgc + o_x_img
            
                x_imgc = self.imgc_gnn_2(x_imgc,edge_index_img)
                x_imgc = self.imgc_relu_2(x_imgc)
            
            batch = torch.zeros(len(x_imgc),dtype=torch.long).to(device)
            pool_x_img_c,att_img_2c = self.mpool_img_c(x_imgc,batch)
            att_2.append(att_img_2c)
            pool_x = torch.cat((pool_x,pool_x_img_c),0)
        if 'rna' in data_type:
            x_rna = self.rna_gnn_2(x_rna,edge_index_rna)
            x_rna = self.rna_relu_2(x_rna)
            batch = torch.zeros(len(x_rna),dtype=torch.long).to(device)
            pool_x_rna,att_rna_2 = self.mpool_rna(x_rna,batch)
            att_2.append(att_rna_2)
            pool_x = torch.cat((pool_x,pool_x_rna),0)
        if 'cli' in data_type:
            x_cli = self.cli_gnn_2(x_cli,edge_index_cli)
            x_cli = self.cli_relu_2(x_cli)
            batch = torch.zeros(len(x_cli),dtype=torch.long).to(device)
            # self.mpool_cli = my_GlobalAttention(att_net_cli)
            pool_x_cli,att_cli_2 = self.mpool_cli(x_cli,batch)
            att_2.append(att_cli_2)
            pool_x = torch.cat((pool_x,pool_x_cli),0)



        # save the features after graph net as 'mae_labels'
        fea_dict['mae_labels'] = pool_x

        # mae
        # it's a transformer and with a masked path
        if len(train_use_type)>1:
            if use_type == train_use_type:
                mae_x = self.mae(pool_x,mask).squeeze(0)
                fea_dict['mae_out'] = mae_x
            else:
                k=0
                tmp_x = torch.zeros((len(train_use_type),pool_x.size(1))).to(device)
                mask = np.ones(len(train_use_type),dtype=bool)
                for i,type_ in enumerate(train_use_type):
                    if type_ in data_type:
                        tmp_x[i] = pool_x[k]
                        k+=1
                        mask[i] = False
                # mask = mask[[0,0,0,1,2]]
                mask = np.expand_dims(mask,0)
                mask = np.expand_dims(mask,0)
                if k==0:
                    mask = np.array([[[False]*len(train_use_type)]])
                mae_x = self.mae(tmp_x,mask).squeeze(0)
                fea_dict['mae_out'] = mae_x
            fea_dict['mask'] = mask

            save_fea['after_mae'] = mae_x.cpu().detach().numpy()
            # mix (特征提取、转置与求和)
            if mix:
                mae_x = self.mix(mae_x)
                save_fea['after_mix'] = mae_x.cpu().detach().numpy()
            # 残差运算：mix后的特征+原特征
            k=0
            
            o_x_img = all_thing.x_img
            o_x_rna = all_thing.x_rna
            o_x_cli = all_thing.x_cli
            
            if 'img' in data_type:
                #o_x_imga = self.img_res_linear(o_x_img)
                #x_img = o_x_imga + mae_x[train_use_type.index('img')]
                #x_img = self.img_res_linear(x_img)
                x_img = x_img + mae_x[train_use_type.index('img')]
                k+=1
            if 'imgb' in data_type:
                #o_x_imgb = self.imgb_res_linear(o_x_img)
                #x_imgb = o_x_imgb + mae_x[train_use_type.index('imgb')]
                #x_imgb = self.imgb_res_linear(x_imgb)
                x_imgb = x_imgb + mae_x[train_use_type.index('imgb')]
                k+=1
            if 'imgc' in data_type:
                #o_x_imgc = self.imgc_res_linear(o_x_img)
                #x_imgc = o_x_imgc + mae_x[train_use_type.index('imgc')]
                #x_imgc = self.imgc_res_linear(x_imgc)
                x_imgc = x_imgc + mae_x[train_use_type.index('imgc')]
                k+=1
            if 'rna' in data_type:
                #o_x_rna = self.rna_res_linear(o_x_rna)
                #x_rna = o_x_rna + mae_x[train_use_type.index('rna')]
                #333x_rna = self.rna_res_linear(x_rna)
                x_rna = x_rna + mae_x[train_use_type.index('rna')]
                k+=1
            if 'cli' in data_type:
                #o_x_cli = self.cli_res_linear(o_x_cli)
                #x_cli = o_x_cli + mae_x[train_use_type.index('cli')]
                #x_cli = self.cli_res_linear(x_cli)
                x_cli = x_cli + mae_x[train_use_type.index('cli')]
                k+=1


        att_3 = []
        pool_x = torch.empty((0)).to(device)
        
        if 'img' in data_type:
            batch = torch.zeros(len(x_img),dtype=torch.long).to(device)
            pool_x_img,att_img_3 = self.mpool_img_2(x_img,batch)
            att_3.append(att_img_3)
            pool_x = torch.cat((pool_x,pool_x_img),0)
        if 'imgb' in data_type:
            batch = torch.zeros(len(x_imgb),dtype=torch.long).to(device)
            pool_x_imgb,att_img_3b = self.mpool_img_2_b(x_imgb,batch)
            att_3.append(att_img_3b)
            pool_x = torch.cat((pool_x,pool_x_imgb),0)
        if 'imgc' in data_type:
            batch = torch.zeros(len(x_imgc),dtype=torch.long).to(device)
            pool_x_imgc,att_img_3c = self.mpool_img_2_c(x_imgc,batch)
            att_3.append(att_img_3c)
            pool_x = torch.cat((pool_x,pool_x_imgc),0)
        if 'rna' in data_type:
            batch = torch.zeros(len(x_rna),dtype=torch.long).to(device)
            pool_x_rna,att_rna_3 = self.mpool_rna_2(x_rna,batch)
            att_3.append(att_rna_3)
            pool_x = torch.cat((pool_x,pool_x_rna),0)
        if 'cli' in data_type:
            batch = torch.zeros(len(x_cli),dtype=torch.long).to(device)
            pool_x_cli,att_cli_3 = self.mpool_cli_2(x_cli,batch)
            att_3.append(att_cli_3)
            pool_x = torch.cat((pool_x,pool_x_cli),0) 

        
        x = pool_x + fea_dict['mae_labels']
        # 取得特征
        x = F.normalize(x, dim=1)
        fea = x
        
        k=0
        if 'img' in data_type:
            fea_dict['img'] = fea[k]
            k+=1
        if 'imgb' in data_type:
            fea_dict['imgb'] = fea[k]
            k+=1
        if 'imgc' in data_type:
            fea_dict['imgc'] = fea[k]
            k+=1
        if 'rna' in data_type:
            fea_dict['rna'] = fea[k]       
            k+=1
        if 'cli' in data_type:
            fea_dict['cli'] = fea[k]
            k+=1

        
        k=0
        multi_x = torch.empty((0)).to(device)
        #rate_multi_x = torch.empty((0)).to(device)
        # 对每个模块做readout部分的MLP运算
        if 'img' in data_type:
            x_img = self.lin1_img(x[k])
            x_img = self.relu(x_img)
            x_img = self.norm_img(x_img)
            x_img = self.dropout(x_img)    

            x_img = self.lin2_img(x_img).unsqueeze(0)
            #print(x_img.shape,self.img_rate.shape)
            #x_img = self.img_rate * x_img
            #print(x_img.shape)
            multi_x = torch.cat((multi_x,x_img),0)
            k+=1
        if 'imgb' in data_type:
            x_imgb = self.lin1_imgb(x[k])
            x_imgb = self.relu(x_imgb)
            x_imgb = self.norm_imgb(x_imgb)
            x_imgb = self.dropout(x_imgb)    

            x_imgb = self.lin2_imgb(x_imgb).unsqueeze(0)
            #print(x_img.shape,self.img_rate.shape)
            #x_imgb = self.img_rate * x_imgb
            #print(x_img.shape)
            multi_x = torch.cat((multi_x,x_imgb),0)
            k+=1
        if 'imgc' in data_type:
            x_imgc = self.lin1_imgc(x[k])
            x_imgc = self.relu(x_imgc)
            x_imgc = self.norm_img(x_imgc)
            x_imgc = self.dropout(x_imgc)    

            x_imgc = self.lin2_img(x_imgc).unsqueeze(0)
            #print(x_img.shape,self.img_rate.shape)
            #x_imgc = self.img_rate * x_imgc
            #print(x_img.shape)
            multi_x = torch.cat((multi_x,x_imgc),0)
            k+=1
        if 'rna' in data_type:
            x_rna = self.lin1_rna(x[k])
            x_rna = self.relu(x_rna)
            x_rna = self.norm_rna(x_rna)
            x_rna = self.dropout(x_rna) 

            x_rna = self.lin2_rna(x_rna).unsqueeze(0)
            
            #x_rna = self.rna_rate * x_rna
            #print(x_rna.shape)
            multi_x = torch.cat((multi_x,x_rna),0)  
            k+=1
        if 'cli' in data_type:
            x_cli = self.lin1_cli(x[k])
            x_cli = self.relu(x_cli)
            x_cli = self.norm_cli(x_cli)
            x_cli = self.dropout(x_cli)

            x_cli = self.lin2_rna(x_cli).unsqueeze(0) 
            
            #x_cli = self.cli_rate * x_cli
            multi_x = torch.cat((multi_x,x_cli),0)  
            k+=1
        # 取均值获得最终所需的特征值
        d_x = torch.mean(multi_x[:3,:]).reshape([1,-1])
        d_x = torch.cat((d_x,multi_x[3:]),0)
        one_x = torch.mean(d_x,dim=0)
        multi_x = torch.cat((torch.mean(multi_x[:3],dim=0).unsqueeze(0), multi_x[3:]),dim=0)
        return (one_x,multi_x),save_fea,(att_2,att_3),fea_dict
        # one_x -> 最终通过均值计算所得多模态特征值
        # multi_x -> 每个模态的最终特征值的集合
        # save_fea -> after mae 与 after mix 的特征值
        # att_2, att_3 -> 图网络后的注意力值与残差运算后的注意力值
        # fea_dict -> mae labels（数据进入mae前）与mae out（mae输出）处的特征值










