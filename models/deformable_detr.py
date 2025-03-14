# ------------------------------------------------------------------------
# Deformable DETR
# Copyright (c) 2020 SenseTime. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 [see LICENSE for details]
# ------------------------------------------------------------------------
# Modified from DETR (https://github.com/facebookresearch/detr)
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
# ------------------------------------------------------------------------

"""
Deformable DETR model and criterion classes.
"""
import torch
import torch.nn.functional as F
from torch import nn
import math
import numpy as np
from util import box_ops
from util.misc import (NestedTensor, nested_tensor_from_tensor_list,
                       accuracy, get_world_size, interpolate,
                       is_dist_avail_and_initialized, inverse_sigmoid)

from .backbone import build_backbone
from .matcher import build_matcher
from .segmentation import (DETRsegm, PostProcessPanoptic, PostProcessSegm,
                           dice_loss, sigmoid_focal_loss)
from .deformable_transformer import build_deforamble_transformer
import copy
from torchvision.ops.boxes import batched_nms 


def _get_clones(module, N):
    return nn.ModuleList([copy.deepcopy(module) for i in range(N)])


class ObjectHead(nn.Module):
    def __init__(self, hidden_dim):
        super().__init__()
        self.flatten = nn.Flatten(0, 1)
        self.linear2 = nn.Linear(hidden_dim, 1)
        self.activate = nn.Sigmoid()
        nn.init.constant_(self.linear2.weight, 0)
        nn.init.constant_(self.linear2.bias, 0)

       
    def fressze_obj_head(self):
        self.obj_head.eval()
    
    def forward(self, x):
        out = self.flatten(x)
        out = self.linear2(out)
        out = self.activate(out)
        out = out.unflatten(0, x.shape[:2])
        return out

class DeformableDETR(nn.Module):
    """ This is the Deformable DETR module that performs object detection """
    def __init__(self, backbone, transformer, num_classes, num_queries, num_feature_levels,
                 aux_loss=True, with_box_refine=False, two_stage=False):
        """ Initializes the model.
        Parameters:
            backbone: torch module of the backbone to be used. See backbone.py
            transformer: torch module of the transformer architecture. See transformer.py
            num_classes: number of object classes
            num_queries: number of object queries, ie detection slot. This is the maximal number of objects
                         DETR can detect in a single image. For COCO, we recommend 100 queries.
            aux_loss: True if auxiliary decoding losses (loss at each decoder layer) are to be used.
            with_box_refine: iterative bounding box refinement
            two_stage: two-stage Deformable DETR
        """
        super().__init__()
        self.num_queries = num_queries
        self.transformer = transformer
        hidden_dim = transformer.d_model
        self.class_embed = nn.Linear(hidden_dim, num_classes)
        self.bbox_embed = MLP(hidden_dim, hidden_dim, 4, 3)
        self.num_feature_levels = num_feature_levels
        #self.object_head = ObjectHead(hidden_dim) 추가해야함
        self.object_head = ObjectHead(hidden_dim)
        

        if not two_stage:
            self.query_embed = nn.Embedding(num_queries, hidden_dim*2)
        if num_feature_levels > 1:
            num_backbone_outs = len(backbone.strides)
            #stride설정한 만큼 사용 할 featuremap 개수임.
            input_proj_list = [] #backbone에서 나오는거.
            for _ in range(num_backbone_outs): #3개
                in_channels = backbone.num_channels[_]
                input_proj_list.append(nn.Sequential(
                    nn.Conv2d(in_channels, hidden_dim, kernel_size=1),
                    nn.GroupNorm(32, hidden_dim), #배치 크기에 의존하지 않고 여러 채널을 그룹으로 나누어 정규화.
                ))
            for _ in range(num_feature_levels - num_backbone_outs):
                input_proj_list.append(nn.Sequential(
                    nn.Conv2d(in_channels, hidden_dim, kernel_size=3, stride=2, padding=1),
                    nn.GroupNorm(32, hidden_dim),
                ))
                in_channels = hidden_dim
            self.input_proj = nn.ModuleList(input_proj_list)
        else:
            self.input_proj = nn.ModuleList([
                nn.Sequential(
                    nn.Conv2d(backbone.num_channels[0], hidden_dim, kernel_size=1),
                    nn.GroupNorm(32, hidden_dim),
                )])
        self.backbone = backbone
        self.aux_loss = aux_loss
        self.with_box_refine = with_box_refine
        self.two_stage = two_stage

        prior_prob = 0.01
        bias_value = -math.log((1 - prior_prob) / prior_prob)
        self.class_embed.bias.data = torch.ones(num_classes) * bias_value
        nn.init.constant_(self.bbox_embed.layers[-1].weight.data, 0)
        nn.init.constant_(self.bbox_embed.layers[-1].bias.data, 0)
        
        for proj in self.input_proj:
            nn.init.xavier_uniform_(proj[0].weight, gain=1)
            nn.init.constant_(proj[0].bias, 0)

        # if two-stage, the last class_embed and bbox_embed is for region proposal generation
        num_pred = (transformer.decoder.num_layers + 1) if two_stage else transformer.decoder.num_layers
        #아마 decoder layer하나 추가해서 region proposal만드는듯
        if with_box_refine:
            self.class_embed = _get_clones(self.class_embed, num_pred)
            self.bbox_embed = _get_clones(self.bbox_embed, num_pred)
            self.object_head =  _get_clones(self.object_head, num_pred)
            #self.bbox_embed = Moduleist
            nn.init.constant_(self.bbox_embed[0].layers[-1].bias.data[2:], -2.0) #첫번째 box_embed의 마지막 출력층. 초기화.
            # hack implementation for iterative bounding box refinement
            self.transformer.decoder.bbox_embed = self.bbox_embed #출력값 결합.
        else:
            nn.init.constant_(self.bbox_embed.layers[-1].bias.data[2:], -2.0)
            self.class_embed = nn.ModuleList([self.class_embed for _ in range(num_pred)])
            self.bbox_embed = nn.ModuleList([self.bbox_embed for _ in range(num_pred)])
            self.object_head = nn.ModuleList([self.object_head for _ in range(num_pred)])
            self.transformer.decoder.bbox_embed = None
        if two_stage:
            # hack implementation for two-stage
            self.transformer.decoder.class_embed = self.class_embed
            self.transformer.decoder.object_head = self.object_head #헤더 다 추가.
            for box_embed in self.bbox_embed: #이미 디코더에 옮겨놓고,0으로 초기화.
                nn.init.constant_(box_embed.layers[-1].bias.data[2:], 0.0)

    def forward(self, samples: NestedTensor):
        """ The forward expects a NestedTensor, which consists of:
               - samples.tensor: batched images, of shape [batch_size x 3 x H x W]
               - samples.mask: a binary mask of shape [batch_size x H x W], containing 1 on padded pixels

            It returns a dict with the following elements:
               - "pred_logits": the classification logits (including no-object) for all queries.
                                Shape= [batch_size x num_queries x (num_classes + 1)]
               - "pred_boxes": The normalized boxes coordinates for all queries, represented as
                               (center_x, center_y, height, width). These values are normalized in [0, 1],
                               relative to the size of each individual image (disregarding possible padding).
                               See PostProcess for information on how to retrieve the unnormalized bounding box.
               - "aux_outputs": Optional, only returned when auxilary losses are activated. It is a list of
                                dictionnaries containing the two above keys for each decoder layer.
        """
        if not isinstance(samples, NestedTensor):
            samples = nested_tensor_from_tensor_list(samples) #NestedTensor로 변환
        features, pos = self.backbone(samples) #backbone에 NestedTensor로 넣어줘야함.

        srcs = []
        masks = []
        for l, feat in enumerate(features):#레벨별로. 3개
            src, mask = feat.decompose()
            srcs.append(self.input_proj[l](src))
            masks.append(mask)
            assert mask is not None
        if self.num_feature_levels > len(srcs):  #if len(srcs)=3,num_feature_levels=5 라면 
            #4개 이상
            _len_srcs = len(srcs) #3
            for l in range(_len_srcs, self.num_feature_levels): #2번
                if l == _len_srcs:
                    src = self.input_proj[l](features[-1].tensors)#4
                else:
                    src = self.input_proj[l](srcs[-1])  
                m = samples.mask
                mask = F.interpolate(m[None].float(), size=src.shape[-2:]).to(torch.bool)[0]
                pos_l = self.backbone[1](NestedTensor(src, mask)).to(src.dtype)
                srcs.append(src)
                #srcs에서 보면 features[2].tensor.shape = [2,2048,34,26] 가 src[3].shape = [2,256,17,13]
                masks.append(mask)
                pos.append(pos_l)

        query_embeds = None
        if not self.two_stage:
            query_embeds = self.query_embed.weight
        hs, init_reference, inter_references, enc_outputs_class, enc_outputs_coord_unact,enc_outputs_obj = self.transformer(srcs, masks, pos, query_embeds)

        outputs_classes = []
        outputs_coords = []
        outputs_objects = [] 
        
        for lvl in range(hs.shape[0]): #각 해상도별로 추출
            if lvl == 0:
                reference = init_reference
            else:
                reference = inter_references[lvl - 1]
            reference = inverse_sigmoid(reference)
            #encoder에서 나온 hs값을 이용하여 class,obj를 출력
            outputs_class = self.class_embed[lvl](hs[lvl])
            outputs_obj = self.object_head[lvl](hs[lvl]) 
            tmp = self.bbox_embed[lvl](hs[lvl]) #출력한 bbox에 reference_point까지 더해줘야함.
            if reference.shape[-1] == 4:
                tmp += reference
            else:
                assert reference.shape[-1] == 2
                tmp[..., :2] += reference
            outputs_coord = tmp.sigmoid()
            outputs_classes.append(outputs_class)
            outputs_coords.append(outputs_coord)
            outputs_objects.append(outputs_obj)
        outputs_class = torch.stack(outputs_classes)
        outputs_coord = torch.stack(outputs_coords)
        outputs_object = torch.stack(outputs_objects)
        
        
        out = {'pred_logits': outputs_class[-1], 'pred_boxes': outputs_coord[-1],'obj':outputs_object[-1]}
        #refinement
        if self.aux_loss: 
            out['aux_outputs'] = self._set_aux_loss(outputs_class, outputs_coord,outputs_object)
            

        if self.two_stage:
            enc_outputs_coord = enc_outputs_coord_unact.sigmoid()
            out['enc_outputs'] = {'pred_logits': enc_outputs_class, 'pred_boxes': enc_outputs_coord,'obj':enc_outputs_obj}
        return out
        #위에 다 추가, set aux loss에.
    @torch.jit.unused
    def _set_aux_loss(self, outputs_class, outputs_coord,outputs_object):#+   output_object
        # this is a workaround to make torchscript happy, as torchscript
        # doesn't support dictionary with non-homogeneous values, such
        # as a dict having both a Tensor and a list.
        return [{'pred_logits': a, 'pred_boxes': b,'obj':c} 
                for a, b ,c in zip(outputs_class[:-1], outputs_coord[:-1],outputs_object[:-1])]


class SetCriterion(nn.Module):
    """ This class computes the loss for DETR.
    The process happens in two steps:
        1) we compute hungarian assignment between ground truth boxes and the outputs of the model
        2) we supervise each pair of matched ground-truth / prediction (supervise class and box)
    """
    def __init__(self, num_classes, matcher, weight_dict, losses, focal_alpha=0.25):
        """ Create the criterion.
        Parameters:
            num_classes: number of object categories, omitting the special no-object category
            matcher: module able to compute a matching between targets and proposals
            weight_dict: dict containing as key the names of the losses and as values their relative weight.
            losses: list of all the losses to be applied. See get_loss for list of available losses.
            focal_alpha: alpha in Focal Loss
        """
        super().__init__()
        self.num_classes = num_classes
        self.matcher = matcher
        self.weight_dict = weight_dict
        self.losses = losses
        self.focal_alpha = focal_alpha

    def loss_labels (self, outputs, targets, indices,second_indices,num_boxes, log=True):
        # matcher는 두번째로 추가하기 .second_indeices loss_label이라 second_indices안씀.
        """Classification loss (NLL)
        targets dicts must contain the key "labels" containing a tensor of dim [nb_target_boxes]
        """
        assert 'pred_logits' in outputs
        src_logits = outputs['pred_logits']
        #indices에 뭐가 들어가있는지 확인하기
        idx = self._get_src_permutation_idx(indices) #다른거 없음. 근데 왜 second_indices는 적용안할까.
        target_classes_o = torch.cat([t["labels"][J] for t, (_, J) in zip(targets, indices)])
        target_classes = torch.full(src_logits.shape[:2], self.num_classes,
                                    dtype=torch.int64, device=src_logits.device)
        target_classes[idx] = target_classes_o

        target_classes_onehot = torch.zeros([src_logits.shape[0], src_logits.shape[1], src_logits.shape[2] + 1],
                                            dtype=src_logits.dtype, layout=src_logits.layout, device=src_logits.device)
        target_classes_onehot.scatter_(2, target_classes.unsqueeze(-1), 1)

        target_classes_onehot = target_classes_onehot[:,:,:-1]
        loss_ce = sigmoid_focal_loss(src_logits, target_classes_onehot, num_boxes, alpha=self.focal_alpha, gamma=2) * src_logits.shape[1]
        losses = {'loss_ce': loss_ce}

        if log:
            # TODO this should probably be a separate loss, not hacked in this one here
            losses['class_error'] = 100 - accuracy(src_logits[idx], target_classes_o)[0]
        return losses

    @torch.no_grad()
    def loss_cardinality(self, outputs, targets, indices,second_indices, num_boxes):#+second_indices
        """ Compute the cardinality error, ie the absolute error in the number of predicted non-empty boxes
        This is not really a loss, it is intended for logging purposes only. It doesn't propagate gradients
        """
        pred_logits = outputs['pred_logits']
        device = pred_logits.device
        tgt_lengths = torch.as_tensor([len(v["labels"]) for v in targets], device=device)
        # Count the number of predictions that are NOT "no-object" (which is the last class)
        card_pred = (pred_logits.argmax(-1) != pred_logits.shape[-1] - 1).sum(1)
        card_err = F.l1_loss(card_pred.float(), tgt_lengths.float())
        losses = {'cardinality_error': card_err}
        return losses

    def loss_boxes(self, outputs, targets, indices,second_indices, num_boxes):
        """Compute the losses related to the bounding boxes, the L1 regression loss and the GIoU loss
           targets dicts must contain the key "boxes" containing a tensor of dim [nb_target_boxes, 4]
           The target boxes are expected in format (center_x, center_y, h, w), normalized by the image size.
        """
        assert 'pred_boxes' in outputs
        idx = self._get_src_permutation_idx(indices)
        src_boxes = outputs['pred_boxes'][idx]
        target_boxes = torch.cat([t['boxes'][i] for t, (_, i) in zip(targets, indices)], dim=0)

        loss_bbox = F.l1_loss(src_boxes, target_boxes, reduction='none')

        losses = {}
        losses['loss_bbox'] = loss_bbox.sum() / num_boxes

        loss_giou = 1 - torch.diag(box_ops.generalized_box_iou(
            box_ops.box_cxcywh_to_xyxy(src_boxes),
            box_ops.box_cxcywh_to_xyxy(target_boxes)))
        losses['loss_giou'] = loss_giou.sum() / num_boxes
        return losses
    
    def calc_objectness(self, src_bbox, gt_bbox):
            c_x, c_y = src_bbox[:2]
            l, t, r, b = gt_bbox[0] - gt_bbox[2] / 2, gt_bbox[1] - gt_bbox[3] / 2, gt_bbox[0] + gt_bbox[2] / 2, gt_bbox[1] + gt_bbox[3] / 2
           
            l_d = torch.abs(c_x - l)
            t_d = torch.abs(c_y - t)
            r_d = torch.abs(r - c_x)
            b_d = torch.abs(b - c_y)
       
            centerness = torch.sqrt((torch.min(l_d, r_d) / torch.max(l_d, r_d)) * (torch.min(t_d, b_d) / torch.max(t_d, b_d)))
            return centerness
    
    
    
    def loss_obj(self, outputs, targets, indices, second_indices, num_boxes):
        assert 'obj' in outputs
        
        
        idx = self._get_src_permutation_idx(indices)
        src_boxes = outputs['pred_boxes'][idx]  # outputs['pred_boxes']'s size [batch, num_queries, 4]
        target_boxes = torch.cat([t['boxes'][i] for t, (_, i) in zip(targets, indices)], dim=0)

        
        src_logits = torch.sigmoid(outputs['pred_logits'][idx])
        first_src_logits_sum = torch.sum(src_logits[:, :20], dim=1)
        
        
        second_idx = self._get_src_permutation_idx(second_indices)
        second_src_boxes = outputs['pred_boxes'][second_idx]
        second_target_boxes = torch.cat([t['boxes'][i] for t, (_, i) in zip(targets, second_indices)], dim=0)
        
        
        src_logits = torch.sigmoid(outputs['pred_logits'][second_idx])
        second_src_logits_sum = torch.sum(src_logits[:, :20], dim=1) 

        first_P_G_GIOU = torch.diag(box_ops.generalized_box_iou(
            box_ops.box_cxcywh_to_xyxy(src_boxes),
            box_ops.box_cxcywh_to_xyxy(target_boxes)))
        
        second_P_G_GIOU = torch.diag(box_ops.generalized_box_iou(
            box_ops.box_cxcywh_to_xyxy(second_src_boxes),
            box_ops.box_cxcywh_to_xyxy(second_target_boxes)))
        
        obj_list = []
        iou_list = []
        negative_list = []
        
        for count, iou in enumerate(first_P_G_GIOU):
            if iou > 0.6:
                iou_list.append(iou * 0.6 + 0.4 * first_src_logits_sum[count])
                obj_list.append(outputs['obj'][idx[0][count]][idx[1][count]])
            else:
                negative_list.append(outputs['obj'][idx[0][count]][idx[1][count]])
    
        for count, iou in enumerate(second_P_G_GIOU):
            if iou > 0.6:
                iou_list.append(iou * 0.6 + 0.4 * second_src_logits_sum[count])
                obj_list.append(outputs['obj'][second_idx[0][count]][second_idx[1][count]])
            else:
                negative_list.append(outputs['obj'][idx[0][count]][idx[1][count]])
        
        if len(obj_list) == 0:
            obj_list.append(outputs['obj'][0][0] - outputs['obj'][0][0])
        if len(negative_list) == 0:
            negative_list.append(outputs['obj'][0][0] - outputs['obj'][0][0])
            
        obj_tensor = torch.cat(obj_list, dim=0)
        iou_tensor = torch.tensor(iou_list).to(src_boxes.device)
        negative_tensor = torch.cat(negative_list, dim=0)
        negative_gt = (torch.ones(negative_tensor.shape) * 0.5).to(src_boxes.device)

        if len(obj_list) == 0:
            losses = {}
            losses['loss_object'] = obj_tensor
        else:
            obj_loss = F.l1_loss(obj_tensor, iou_tensor, reduction='none')
            neg_loss = F.l1_loss(negative_tensor, negative_gt, reduction='none')
            losses = {}
            losses['loss_object'] = obj_loss.sum() / num_boxes + neg_loss.sum() / num_boxes
        return losses

    def loss_eobj(self, outputs, targets, indices, second_indices, num_boxes):
        assert 'obj' in outputs
        
        
        idx = self._get_src_permutation_idx(indices)
        src_boxes = outputs['pred_boxes'][idx]  # outputs['pred_boxes']'s size [batch, num_queries, 4]
        target_boxes = torch.cat([t['boxes'][i] for t, (_, i) in zip(targets, indices)], dim=0)

        
        src_logits = torch.sigmoid(outputs['pred_logits'][idx])
        first_src_logits_sum = torch.sum(src_logits[:, :20], dim=1)
        
        
        second_idx = self._get_src_permutation_idx(second_indices)
        second_src_boxes = outputs['pred_boxes'][second_idx]
        second_target_boxes = torch.cat([t['boxes'][i] for t, (_, i) in zip(targets, second_indices)], dim=0)
        
        
        src_logits = torch.sigmoid(outputs['pred_logits'][second_idx])
        second_src_logits_sum = torch.sum(src_logits[:, :20], dim=1) 

        first_P_G_GIOU = torch.diag(box_ops.generalized_box_iou(
            box_ops.box_cxcywh_to_xyxy(src_boxes),
            box_ops.box_cxcywh_to_xyxy(target_boxes)))
        
        second_P_G_GIOU = torch.diag(box_ops.generalized_box_iou(
            box_ops.box_cxcywh_to_xyxy(second_src_boxes),
            box_ops.box_cxcywh_to_xyxy(second_target_boxes)))
        
        obj_list = []
        iou_list = []
        negative_list = []
        
        for count, iou in enumerate(first_P_G_GIOU):
            if iou > 0.6:
                iou_list.append(first_src_logits_sum[count])
                obj_list.append(outputs['obj'][idx[0][count]][idx[1][count]])
            else:
                negative_list.append(outputs['obj'][idx[0][count]][idx[1][count]])
    
        for count, iou in enumerate(second_P_G_GIOU):
            if iou > 0.6:
                iou_list.append(second_src_logits_sum[count])
                obj_list.append(outputs['obj'][second_idx[0][count]][second_idx[1][count]])
            else:
                negative_list.append(outputs['obj'][idx[0][count]][idx[1][count]])
        
        if len(obj_list) == 0:
            obj_list.append(outputs['obj'][0][0] - outputs['obj'][0][0])
        if len(negative_list) == 0:
            negative_list.append(outputs['obj'][0][0] - outputs['obj'][0][0])
            
        obj_tensor = torch.cat(obj_list, dim=0)
        iou_tensor = torch.tensor(iou_list).to(src_boxes.device)
        negative_tensor = torch.cat(negative_list, dim=0)
        negative_gt = (torch.ones(negative_tensor.shape) * 0.5).to(src_boxes.device)

        if len(obj_list) == 0:
            losses = {}
            losses['loss_eobject'] = obj_tensor
        else:
            obj_loss = F.l1_loss(obj_tensor, iou_tensor, reduction='none')
            neg_loss = F.l1_loss(negative_tensor, negative_gt, reduction='none')
            losses = {}
            losses['loss_eobject'] = obj_loss.sum() / num_boxes + neg_loss.sum() / num_boxes
        return losses



    def loss_masks(self, outputs, targets, indices, num_boxes):#tmp_indices,epoch
        """Compute the losses related to the masks: the focal loss and the dice loss.
           targets dicts must contain the key "masks" containing a tensor of dim [nb_target_boxes, h, w]
        """
        assert "pred_masks" in outputs

        src_idx = self._get_src_permutation_idx(indices)
        tgt_idx = self._get_tgt_permutation_idx(indices)

        src_masks = outputs["pred_masks"]

        # TODO use valid to mask invalid areas due to padding in loss
        target_masks, valid = nested_tensor_from_tensor_list([t["masks"] for t in targets]).decompose()
        target_masks = target_masks.to(src_masks)

        src_masks = src_masks[src_idx]
        # upsample predictions to the target size
        src_masks = interpolate(src_masks[:, None], size=target_masks.shape[-2:],
                                mode="bilinear", align_corners=False)
        src_masks = src_masks[:, 0].flatten(1)

        target_masks = target_masks[tgt_idx].flatten(1)

        losses = {
            "loss_mask": sigmoid_focal_loss(src_masks, target_masks, num_boxes),
            "loss_dice": dice_loss(src_masks, target_masks, num_boxes),
        }
        return losses

    def _get_src_permutation_idx(self, indices):
        # permute predictions following indices
        batch_idx = torch.cat([torch.full_like(src, i) for i, (src, _) in enumerate(indices)])
        src_idx = torch.cat([src for (src, _) in indices])
        return batch_idx, src_idx

    def _get_tgt_permutation_idx(self, indices):
        # permute targets following indices
        batch_idx = torch.cat([torch.full_like(tgt, i) for i, (_, tgt) in enumerate(indices)])
        tgt_idx = torch.cat([tgt for (_, tgt) in indices])
        return batch_idx, tgt_idx

    def get_loss(self, loss, outputs, targets,pos_indices, neg_indices, num_boxes, **kwargs):#pos_indices,neg_indices 추가하기. return 값도 동일하게!
        loss_map = {
            'labels': self.loss_labels,
            'cardinality': self.loss_cardinality,
            'boxes': self.loss_boxes,
            'objs': self.loss_obj,
            'eobjs': self.loss_eobj,
            'masks': self.loss_masks
        }
        #obj,eobj추가해야함. 
        #딕셔너리와 메소드를 매핑시켜줌.
        assert loss in loss_map, f'do you really want to compute {loss} loss?'
        return loss_map[loss](outputs, targets, pos_indices,neg_indices, num_boxes, **kwargs)
    
    def forward(self, outputs, targets):
        """ This performs the loss computation.
        Parameters:
             outputs: dict of tensors, see the output specification of the model for the format
             targets: list of dicts, such that len(targets) == batch_size.
                      The expected keys in each dict depends on the losses applied, see each loss' doc
        """
        outputs_without_aux = {k: v for k, v in outputs.items() if k != 'aux_outputs' and k != 'enc_outputs'}

        # Retrieve the matching between the outputs of the last layer and the targets
        indices, second_indices = self.matcher(outputs_without_aux, targets)
        #second_indices도 받기!
        
        # Compute the average number of target boxes accross all nodes, for normalization purposes
        num_boxes = sum(len(t["labels"]) for t in targets)
        num_boxes = torch.as_tensor([num_boxes], dtype=torch.float, device=next(iter(outputs.values())).device)
        if is_dist_avail_and_initialized():
            torch.distributed.all_reduce(num_boxes)
        num_boxes = torch.clamp(num_boxes / get_world_size(), min=1).item()

        # Compute all the requested losses
        losses = {}
        for loss in self.losses:
            kwargs = {}
            losses.update(self.get_loss(loss, outputs, targets, indices,second_indices, num_boxes, **kwargs))
            #second_indices도 추가하기

        # In case of auxiliary losses, we repeat this process with the output of each intermediate layer.
        if 'aux_outputs' in outputs:
            for i, aux_outputs in enumerate(outputs['aux_outputs']):
                indices,second_indices = self.matcher(aux_outputs, targets) #여기도 second indices
                for loss in self.losses:
                    if loss == 'masks':
                        # Intermediate masks losses are too costly to compute, we ignore them.
                        continue
                    kwargs = {}
                    if loss == 'labels':
                        # Logging is enabled only for the last layer
                        kwargs['log'] = False
                    l_dict = self.get_loss(loss, aux_outputs, targets, indices,second_indices, num_boxes, **kwargs)
                    #왜 나눠서? 아 어느 레이어 출력인지 알려고!
                    l_dict = {k + f'_{i}': v for k, v in l_dict.items()}
                    losses.update(l_dict)

        if 'enc_outputs' in outputs:
            enc_outputs = outputs['enc_outputs']
            bin_targets = copy.deepcopy(targets)
            for bt in bin_targets:
                bt['labels'] = torch.zeros_like(bt['labels'])
            indices ,second_indices = self.matcher(enc_outputs, bin_targets) #여기서도 second indices
            for loss in self.losses:
                if loss == 'masks' or loss == 'objs': #objs추가함
                    # Intermediate masks losses are too costly to compute, we ignore them.
                    continue
                kwargs = {}
                if loss == 'labels':
                    # Logging is enabled only for the last layer
                    kwargs['log'] = False
                l_dict = self.get_loss(loss, enc_outputs, bin_targets, indices,second_indices, num_boxes, **kwargs)
                #secondies도 
                l_dict = {k + f'_enc': v for k, v in l_dict.items()}
                losses.update(l_dict)

        return losses

class PostProcess(nn.Module):
    """ This module converts the model's output into the format expected by the coco api"""
 #coco format으로 변환하는 모듈
    @torch.no_grad()
    def forward(self, outputs, target_sizes):
        """ Perform the computation
        Parameters:
            outputs: raw outputs of the model
            target_sizes: tensor of dimension [batch_size x 2] containing the size of each images of the batch
                          For evaluation, this must be the original image size (before any data augmentation)
                          For visualization, this should be the image size after data augment, but before padding
        """
        #outputs은 모델의 raw output
        out_logits, out_bbox = outputs['pred_logits'], outputs['pred_boxes']
        out_objectness = outputs['obj']
        assert len(out_logits) == len(target_sizes)
        assert target_sizes.shape[1] == 2

        prob = out_logits.sigmoid()
        topk_values, topk_indexes = torch.topk(prob.view(out_logits.shape[0], -1), 100, dim=1)
        scores = topk_values
        topk_boxes = topk_indexes // out_logits.shape[2] #indexes는 flattent한 상태라 원래 행방향으로 변환.
        labels = topk_indexes % out_logits.shape[2]
        boxes = box_ops.box_cxcywh_to_xyxy(out_bbox)
        boxes = torch.gather(boxes, 1, topk_boxes.unsqueeze(-1).repeat(1,1,4))
        #unsqueeze(-1)는 마지막 차원 확장
        # and from relative [0, 1] to absolute [0, height] coordinates
        img_h, img_w = target_sizes.unbind(1)
        scale_fct = torch.stack([img_w, img_h, img_w, img_h], dim=1)
        boxes = boxes * scale_fct[:, None, :]

        results = [{'scores': s, 'labels': l, 'boxes': b} for s, l, b in zip(scores, labels, boxes)]

        return results

#class NMSPostProcess(nn.Module):추가.
class NMSPostProcess(nn.Module):
    """ This module converts the model's output into the format expected by the coco api"""

    @torch.no_grad()
    def forward(self, outputs, target_sizes):
        """ Perform the computation
        Parameters:
            outputs: raw outputs of the model
            target_sizes: tensor of dimension [batch_size x 2] containing the size of each images of the batch
                          For evaluation, this must be the original image size (before any data augmentation)
                          For visualization, this should be the image size after data augment, but before padding
        """
        
        print('use nms process')
        
        out_logits, out_bbox = outputs['pred_logits'], outputs['pred_boxes']
        bs, n_queries, n_cls = out_logits.shape
        #query는 객체의 수, n_cls는 class의 수
        assert len(out_logits) == len(target_sizes)
        assert target_sizes.shape[1] == 2

        prob = out_logits.sigmoid()

        all_scores = prob.view(bs, n_queries * n_cls).to(out_logits.device)
        all_indexes = torch.arange(n_queries * n_cls)[None].repeat(bs, 1).to(out_logits.device)
        all_boxes = all_indexes // out_logits.shape[2]
        all_labels = all_indexes % out_logits.shape[2]
#out_bbox ,all_indexes //랑 %랑 값이 어떻게?

        boxes = box_ops.box_cxcywh_to_xyxy(out_bbox)
        boxes = torch.gather(boxes, 1, all_boxes.unsqueeze(-1).repeat(1,1,4))

        # and from relative [0, 1] to absolute [0, height] coordinates
        img_h, img_w = target_sizes.unbind(1)
        scale_fct = torch.stack([img_w, img_h, img_w, img_h], dim=1)
        boxes = boxes * scale_fct[:, None, :]

        results = []
        for b in range(bs):
            box = boxes[b]
            score = all_scores[b]
            lbls = all_labels[b]

            if n_queries * n_cls > 10000:
                pre_topk = score.topk(10000).indices
                box = box[pre_topk]
                score = score[pre_topk]
                lbls = lbls[pre_topk]

            keep_inds = batched_nms(box, score, lbls, 0.7)[:100]
            results.append({
                'scores': score[keep_inds],
                'labels': lbls[keep_inds],
                'boxes':  box[keep_inds],
            })

        return results
    #이거로 바꿔보기.
class MLP(nn.Module):
    """ Very simple multi-layer perceptron (also called FFN)"""

    def __init__(self, input_dim, hidden_dim, output_dim, num_layers):
        super().__init__()
        self.num_layers = num_layers
        h = [hidden_dim] * (num_layers - 1)
        self.layers = nn.ModuleList(nn.Linear(n, k) for n, k in zip([input_dim] + h, h + [output_dim]))
        #nn.Linear로 레이어를 쌓음
    def forward(self, x):
        for i, layer in enumerate(self.layers): #마지막층 아닐때만 그냥 출력.
            x = F.relu(layer(x)) if i < self.num_layers - 1 else layer(x)
        return x


def build(args):
    num_classes = 20 if args.dataset_file != 'coco' else 91
    if args.dataset_file == "coco_panoptic":
        num_classes = 250
        #여기서는 20으로함.
    device = torch.device(args.device)

    backbone = build_backbone(args)

    transformer = build_deforamble_transformer(args)
    model = DeformableDETR(
        backbone,
        transformer,
        num_classes=num_classes,
        num_queries=args.num_queries,
        num_feature_levels=args.num_feature_levels,
        aux_loss=args.aux_loss,
        with_box_refine=args.with_box_refine,
        two_stage=args.two_stage,
    ) 
    if args.masks:
        model = DETRsegm(model, freeze_detr=(args.frozen_weights is not None))
    
    matcher = build_matcher(args) 
    
    weight_dict = {'loss_ce': args.cls_loss_coef, 'loss_bbox': args.bbox_loss_coef,'loss_object': args.obj_loss_coef}
    
    weight_dict['loss_giou'] = args.giou_loss_coef
    weight_dict['loss_eobject'] = args.obj_loss_coef
    if args.masks:
        weight_dict["loss_mask"] = args.mask_loss_coef
        weight_dict["loss_dice"] = args.dice_loss_coef
    # TODO this is a hack
    if args.aux_loss:
        aux_weight_dict = {}
        for i in range(args.dec_layers - 1):
            aux_weight_dict.update({k + f'_{i}': v for k, v in weight_dict.items()})
        aux_weight_dict.update({k + f'_enc': v for k, v in weight_dict.items()})
        weight_dict.update(aux_weight_dict)

    losses = ['labels', 'boxes', 'cardinality','objs','eobjs'] #obj,eobj추가
    if args.masks:
        losses += ["masks"]
    # num_classes, matcher, weight_dict, losses, focal_alpha=0.25
    criterion = SetCriterion(num_classes, matcher, weight_dict, losses, focal_alpha=args.focal_alpha)
    criterion.to(device)
    postprocessors = {'bbox': PostProcess()} #이부분바꿔보기!
    if args.masks:
        postprocessors['segm'] = PostProcessSegm()
        if args.dataset_file == "coco_panoptic":
            is_thing_map = {i: i <= 90 for i in range(201)}
            postprocessors["panoptic"] = PostProcessPanoptic(is_thing_map, threshold=0.85)

    return model, criterion, postprocessors
