import os, pdb
import glob
import tqdm
import math
import imageio
import random
import warnings
import tensorboardX

import numpy as np
import pandas as pd

import time
from datetime import datetime

import cv2
import matplotlib.pyplot as plt

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torch.distributed as dist
from torch.utils.data import Dataset, DataLoader

import trimesh
from rich.console import Console
from torch_ema import ExponentialMovingAverage

from packaging import version as pver

import torchvision.transforms as T
import numpy as np
import cv2
from PIL import Image

from nerf.provider import rand_poses
# from nerf.diffaug import DiffAugment

from torch_efficient_distloss import eff_distloss

from kornia.losses import ssim_loss, inverse_depth_smoothness_loss, total_variation
from kornia.filters import gaussian_blur2d

def visualize_depth(depth, cmap=cv2.COLORMAP_JET):
    """
    depth: (H, W)
    """
    x = depth.cpu().numpy()
    x = np.nan_to_num(x) # change nan to 0
    mi = np.min(x) # get minimum depth
    ma = np.max(x)
    x = (x-mi)/(ma-mi+1e-8) # normalize to 0~1
    x = (255*x).astype(np.uint8)
    x_ = Image.fromarray(cv2.applyColorMap(x, cmap))
    x_ = T.ToTensor()(x_) # (3, H, W)
    return x_

def custom_meshgrid(*args):
    # ref: https://pytorch.org/docs/stable/generated/torch.meshgrid.html?highlight=meshgrid#torch.meshgrid
    if pver.parse(torch.__version__) < pver.parse('1.10'):
        return torch.meshgrid(*args)
    else:
        return torch.meshgrid(*args, indexing='ij')

def safe_normalize(x, eps=1e-20):
    return x / torch.sqrt(torch.clamp(torch.sum(x * x, -1, keepdim=True), min=eps))

@torch.cuda.amp.autocast(enabled=False)
def get_rays(poses, intrinsics, H, W, N=-1, error_map=None):
    ''' get rays
    Args:
        poses: [B, 4, 4], cam2world
        intrinsics: [4]
        H, W, N: int
        error_map: [B, 128 * 128], sample probability based on training error
    Returns:
        rays_o, rays_d: [B, N, 3]
        inds: [B, N]
    '''

    device = poses.device
    B = poses.shape[0]
    fx, fy, cx, cy = intrinsics

    i, j = custom_meshgrid(torch.linspace(0, W-1, W, device=device), torch.linspace(0, H-1, H, device=device))
    i = i.t().reshape([1, H*W]).expand([B, H*W]) + 0.5
    j = j.t().reshape([1, H*W]).expand([B, H*W]) + 0.5

    results = {}

    if N > 0:
        N = min(N, H*W)

        if error_map is None:
            inds = torch.randint(0, H*W, size=[N], device=device) # may duplicate
            inds = inds.expand([B, N])
        else:

            # weighted sample on a low-reso grid
            inds_coarse = torch.multinomial(error_map.to(device), N, replacement=False) # [B, N], but in [0, 128*128)

            # map to the original resolution with random perturb.
            inds_x, inds_y = inds_coarse // 128, inds_coarse % 128 # `//` will throw a warning in torch 1.10... anyway.
            sx, sy = H / 128, W / 128
            inds_x = (inds_x * sx + torch.rand(B, N, device=device) * sx).long().clamp(max=H - 1)
            inds_y = (inds_y * sy + torch.rand(B, N, device=device) * sy).long().clamp(max=W - 1)
            inds = inds_x * W + inds_y

            results['inds_coarse'] = inds_coarse # need this when updating error_map

        i = torch.gather(i, -1, inds)
        j = torch.gather(j, -1, inds)

        results['inds'] = inds

    else:
        inds = torch.arange(H*W, device=device).expand([B, H*W])

    zs = torch.ones_like(i)
    # xs = -(i - cx) / fx * zs
    xs = (i - cx) / fx * zs
    ys = (j - cy) / fy * zs
    directions = torch.stack((xs, ys, zs), dim=-1)
    directions = safe_normalize(directions)
    rays_d = directions @ poses[:, :3, :3].transpose(-1, -2) # (B, N, 3)

    rays_o = poses[..., :3, 3] # [B, 3]
    rays_o = rays_o[..., None, :].expand_as(rays_d) # [B, N, 3]

    results['rays_o'] = rays_o
    results['rays_d'] = rays_d

    return results


def seed_everything(seed):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    #torch.backends.cudnn.deterministic = True
    #torch.backends.cudnn.benchmark = True


def torch_vis_2d(x, renormalize=False):
    # x: [3, H, W] or [1, H, W] or [H, W]
    import matplotlib.pyplot as plt
    import numpy as np
    import torch
    
    if isinstance(x, torch.Tensor):
        if len(x.shape) == 3:
            x = x.permute(1,2,0).squeeze()
        x = x.detach().cpu().numpy()
        
    print(f'[torch_vis_2d] {x.shape}, {x.dtype}, {x.min()} ~ {x.max()}')
    
    x = x.astype(np.float32)
    
    # renormalize
    if renormalize:
        x = (x - x.min(axis=0, keepdims=True)) / (x.max(axis=0, keepdims=True) - x.min(axis=0, keepdims=True) + 1e-8)

    plt.imshow(x)
    plt.show()

@torch.jit.script
def linear_to_srgb(x):
    return torch.where(x < 0.0031308, 12.92 * x, 1.055 * x ** 0.41666 - 0.055)


@torch.jit.script
def srgb_to_linear(x):
    return torch.where(x < 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)

from nerf.clip import CLIP

def prepare(im, h, w, interp=None):
    return cv2.resize(im, (w, h))
    hh, ww = im.shape[:2]
    if len(im.shape) == 3:
        # im = im[hh // 2 - h // 2:hh // 2 + h // 2, ww // 2 - w // 2:ww // 2 + w // 2, :]
        im = im[hh // 2 - 128:hh // 2 + 128, ww // 2 - 128:ww // 2 + 128, :]
    else:
        im = im[hh // 2 - 128:hh // 2 + 128, ww // 2 - 128:ww // 2 + 128]
    if interp is not None:
        return cv2.resize(im, (128, 128), interp)
    return cv2.resize(im, (128, 128))

class Trainer(object):
    def __init__(self, 
                 name, # name of this experiment
                 opt, # extra conf
                 model, # network 
                 guidance, # guidance network
                 criterion=None, # loss function, if None, assume inline implementation in train_step
                 optimizer=None, # optimizer
                 ema_decay=None, # if use EMA, set the decay
                 lr_scheduler=None, # scheduler
                 metrics=[], # metrics for evaluation, if None, use val_loss to measure performance, else use the first metric.
                 local_rank=0, # which GPU am I
                 world_size=1, # total num of GPUs
                 device=None, # device to use, usually setting to None is OK. (auto choose device)
                 mute=False, # whether to mute all print
                 fp16=False, # amp optimize level
                 eval_interval=1, # eval once every $ epoch
                 max_keep_ckpt=2, # max num of saved ckpts in disk
                 workspace='workspace', # workspace to save logs & ckpts
                 best_mode='min', # the smaller/larger result, the better
                 use_loss_as_metric=True, # use loss as the first metric
                 report_metric_at_train=False, # also report metrics at training
                 use_checkpoint="latest", # which ckpt to use at init time
                 use_tensorboardX=True, # whether to use tensorboard for logging
                 scheduler_update_every_step=False, # whether to call scheduler.step() after every train step
                 ):
        
        self.name = name
        self.opt = opt
        self.mute = mute
        self.metrics = metrics
        self.local_rank = local_rank
        self.world_size = world_size
        self.workspace = workspace
        self.ema_decay = ema_decay
        self.fp16 = fp16
        self.best_mode = best_mode
        self.use_loss_as_metric = use_loss_as_metric
        self.report_metric_at_train = report_metric_at_train
        self.max_keep_ckpt = max_keep_ckpt
        self.eval_interval = eval_interval
        self.use_checkpoint = use_checkpoint
        self.use_tensorboardX = use_tensorboardX
        self.time_stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        self.scheduler_update_every_step = scheduler_update_every_step
        self.device = device if device is not None else torch.device(f'cuda:{local_rank}' if torch.cuda.is_available() else 'cpu')
        self.console = Console()
    
        model.to(self.device)
        if self.world_size > 1:
            model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[local_rank])
        self.model = model

        # guide model
        self.guidance = guidance
        self.clip = CLIP(device)

        for p in self.clip.parameters():
            p.requires_grad = False

        # text prompt
        if self.guidance is not None:
            
            for p in self.guidance.parameters():
                p.requires_grad = False

            self.prepare_text_embeddings()
        
        else:
            self.text_z = None
    
        if isinstance(criterion, nn.Module):
            criterion.to(self.device)
        self.criterion = criterion

        if optimizer is None:
            self.optimizer = optim.Adam(self.model.parameters(), lr=0.001, weight_decay=5e-4) # naive adam
        else:
            self.optimizer = optimizer(self.model)

        if lr_scheduler is None:
            self.lr_scheduler = optim.lr_scheduler.LambdaLR(self.optimizer, lr_lambda=lambda epoch: 1) # fake scheduler
        else:
            self.lr_scheduler = lr_scheduler(self.optimizer)

        if ema_decay is not None:
            self.ema = ExponentialMovingAverage(self.model.parameters(), decay=ema_decay)
        else:
            self.ema = None

        self.scaler = torch.cuda.amp.GradScaler(enabled=self.fp16)

        # variable init
        self.epoch = 0
        self.global_step = 0
        self.local_step = 0
        self.stats = {
            "loss": [],
            "valid_loss": [],
            "results": [], # metrics[0], or valid_loss
            "checkpoints": [], # record path of saved ckpt, to automatically remove old ckpt
            "best_result": None,
        }

        # auto fix
        if len(metrics) == 0 or self.use_loss_as_metric:
            self.best_mode = 'min'

        # workspace prepare
        self.log_ptr = None
        if self.workspace is not None:
            os.makedirs(self.workspace, exist_ok=True)        
            self.log_path = os.path.join(workspace, f"log_{self.name}.txt")
            self.log_ptr = open(self.log_path, "a+")

            self.ckpt_path = os.path.join(self.workspace, 'checkpoints')
            self.best_path = f"{self.ckpt_path}/{self.name}.pth"
            os.makedirs(self.ckpt_path, exist_ok=True)
            
        self.log(f'[INFO] Trainer: {self.name} | {self.time_stamp} | {self.device} | {"fp16" if self.fp16 else "fp32"} | {self.workspace}')
        self.log(f'[INFO] #parameters: {sum([p.numel() for p in model.parameters() if p.requires_grad])}')

        if self.workspace is not None:
            if self.use_checkpoint == "scratch":
                self.log("[INFO] Training from scratch ...")
            elif self.use_checkpoint == "latest":
                self.log("[INFO] Loading latest checkpoint ...")
                self.load_checkpoint()
            elif self.use_checkpoint == "latest_model":
                self.log("[INFO] Loading latest checkpoint (model only)...")
                self.load_checkpoint(model_only=True)
            elif self.use_checkpoint == "best":
                if os.path.exists(self.best_path):
                    self.log("[INFO] Loading best checkpoint ...")
                    self.load_checkpoint(self.best_path)
                else:
                    self.log(f"[INFO] {self.best_path} not found, loading latest ...")
                    self.load_checkpoint()
            else: # path to ckpt
                self.log(f"[INFO] Loading {self.use_checkpoint} ...")
                self.load_checkpoint(self.use_checkpoint)
        
        self.gaussian_blur = T.GaussianBlur(15, sigma=(0.1, 10))

    # calculate the text embs.
    def prepare_text_embeddings(self):

        if self.opt.text is None:
            self.log(f"[WARN] text prompt is not provided.")
            self.text_z = None
            return

        if not self.opt.dir_text:
            self.text_z = self.guidance.get_text_embeds([self.opt.text], [self.opt.negative])
            self.text_z_clip = self.clip.get_text_embeds(self.opt.text)
        else:
            self.text_z = []
            self.text_z_clip = []
            for d in ['front', 'side', 'back', 'side', 'overhead', 'bottom']:
                # construct dir-encoded text
                text = f"{self.opt.text}, {d} view"

                negative_text = f"{self.opt.negative}"

                # explicit negative dir-encoded text
                if self.opt.negative_dir_text:
                    if negative_text != '': negative_text += ', '

                    if d == 'back': negative_text += "front view"
                    elif d == 'front': negative_text += "back view"
                    elif d == 'side': negative_text += "front view, back view"
                    elif d == 'overhead': negative_text += "bottom view"
                    elif d == 'bottom': negative_text += "overhead view"

                text_z = self.guidance.get_text_embeds([text], [negative_text], dir=d)
                text_z_clip = self.clip.get_text_embeds(text)
                self.text_z.append(text_z)
                self.text_z_clip.append(text_z_clip)
            text_z = self.guidance.get_text_embeds([self.opt.text], [f"{self.opt.negative}"], dir=d)
            text_z_clip = self.clip.get_text_embeds(self.opt.text)
            self.text_z.append(text_z)
            self.text_z_clip.append(text_z_clip)


        mask = cv2.imread(self.opt.mask_path, 0) / 255
        mask[mask > 0.5] = 1
        mask[mask < 0.5] = 0
        mask = prepare(mask, self.opt.h, self.opt.w, cv2.INTER_NEAREST)
        # mask = cv2.resize(mask, (self.opt.h, self.opt.w), interpolation = cv2.INTER_NEAREST)
        mask = torch.from_numpy(mask).float()
        self.fg_mask_2d = mask.cuda().unsqueeze(0).unsqueeze(0) # 1, 1, h, w

        im = Image.open(self.opt.rgb_path).convert('RGB')
        # im = im.resize((self.opt.h, self.opt.w))
        im = np.array(im)
        im = prepare(im, self.opt.h, self.opt.w)
        # im = im[:, :, :-1] #rgba
        self.rgb = torch.tensor(im, requires_grad=False).float().permute(2, 0, 1).unsqueeze(0) / 255#.unsqueeze(0)
        self.rgb = self.rgb.cuda() # 1, 3, h, w
        self.rgb = self.rgb * self.fg_mask_2d
        self.rgb_unmasked = self.rgb.clone()
        self.rgb_clip_embed = self.clip.get_img_embeds(self.rgb_unmasked)

        if 'npy' in self.opt.depth_path:
            im = np.load(self.opt.depth_path)
        else:
            raise NotImplementedError
        im = prepare(im, self.opt.h, self.opt.w, cv2.INTER_NEAREST)
        self.depth = torch.FloatTensor(im).float()#.unsqueeze(0)
        self.depth = self.depth.cuda() # h, w
        self.depth = self.depth * self.fg_mask_2d # make bg region depth 0
        print('depth nonzero range', self.depth.min(), self.depth[self.depth > 0].min(), self.depth.max())
        tee = self.depth.reshape(-1)
        self.fg_idx = tee.nonzero()
        # fg_depth = tee
        tee = tee[self.fg_idx]
        # print('?????', self.fg_idx.shape, tee.shape, self.depth.reshape(-1).shape)
        tee = tee.reshape(1, -1)
        self.rank_loss_target = (tee - tee.T).sign().reshape(-1)

    def margin_rank_loss(self, depth):
        # high res, only calc on fg
        output = depth.squeeze().view(-1)
        output = output[self.fg_idx]
        num = output.shape[0] # [n, 1]
        # print(num)
        output = output.reshape(1, -1)
        o1 = output.expand(num, -1).reshape(-1)
        o2 = output.T.expand(-1, num).reshape(-1)
        return F.margin_ranking_loss(o1, o2, self.rank_loss_target)
    
    def __del__(self):
        if self.log_ptr: 
            self.log_ptr.close()


    def log(self, *args, **kwargs):
        if self.local_rank == 0:
            if not self.mute: 
                #print(*args)
                self.console.print(*args, **kwargs)
            if self.log_ptr: 
                print(*args, file=self.log_ptr)
                self.log_ptr.flush() # write immediately to file

    ### ------------------------------	

    def train_step(self, data):

        if self.epoch <= self.opt.warmup_epoch or (np.random.random() < max(self.opt.front_ratio, 0.1 - (self.epoch - self.opt.warmup_epoch) / (self.opt.max_epoch - self.opt.warmup_epoch))):
            self.front_view = True
        else:
            self.front_view = False

        if self.front_view:
            # horizontal, front view
            poses, dirs = rand_poses(1, self.device, radius_range=[self.opt.init_radius, self.opt.init_radius], return_dirs=self.opt.dir_text, theta_range=[self.opt.init_theta, self.opt.init_theta], phi_range=[180, 180], jitter=False, angle_overhead=self.opt.angle_overhead, angle_front=self.opt.angle_front, uniform_sphere_rate=0)
            fov = self.opt.front_fov
            focal = self.opt.h / (2 * np.tan(np.deg2rad(fov) / 2))
            intrinsics = np.array([focal, focal, self.opt.h / 2, self.opt.w / 2])
            rays = get_rays(poses, intrinsics, self.opt.h, self.opt.w, -1)
            rays_o = rays['rays_o'].cuda() # [B, N, 3]
            rays_d = rays['rays_d'].cuda() # [B, N, 3]
            shading = 'albedo'
            ambient_ratio = 1.0
            l_p = 0
            l_a = 1

            B, N = rays_o.shape[:2]
            H, W = data['H'], data['W']
            data['dir'] = 0
        else:
            rays_o = data['rays_o'] # [B, N, 3]
            rays_d = data['rays_d'] # [B, N, 3]

            B, N = rays_o.shape[:2]
            H, W = data['H'], data['W']

            if random.random() < self.opt.ref_perturb_prob:
                # near by view
                poses, dirs = rand_poses(1, self.device, radius_range=[self.opt.init_radius, self.opt.init_radius], return_dirs=self.opt.dir_text, theta_range=[self.opt.init_theta, self.opt.init_theta], phi_range=[180, 180], jitter=True, angle_overhead=self.opt.angle_overhead, angle_front=self.opt.angle_front, uniform_sphere_rate=0)
                # poses += torch.randn(3, device=rays_o.device, dtype=torch.float)
                fov = random.random() * (self.opt.fovy_range[1] - self.opt.fovy_range[0]) + self.opt.fovy_range[0]
                focal = self.opt.h / (2 * np.tan(np.deg2rad(fov) / 2))
                intrinsics = np.array([focal, focal, self.opt.h / 2, self.opt.w / 2])
                rays = get_rays(poses, intrinsics, self.opt.h, self.opt.w, -1)
                rays_o = rays['rays_o'].cuda() # [B, N, 3]
                rays_d = rays['rays_d'].cuda() # [B, N, 3]
                data['dir'] = dirs

            if self.global_step < self.opt.albedo_iters:
                shading = 'albedo'
                ambient_ratio = 1.0
                l_p = 0
                l_a = 1
            else: 
                rand = random.random()
                if rand < self.opt.p_albedo:
                    shading = 'albedo'
                    ambient_ratio = 1.0
                    l_a = torch.ones(3, device=rays_o.device, dtype=torch.float)
                    l_p = torch.zeros(3, device=rays_o.device, dtype=torch.float)
                else:
                    # re-sample pose for normal (low resolution)
                    if random.random() < self.opt.ref_perturb_prob:
                        # near by view
                        poses, dirs = rand_poses(1, self.device, radius_range=[self.opt.init_radius, self.opt.init_radius], return_dirs=self.opt.dir_text, theta_range=[self.opt.init_theta, self.opt.init_theta], phi_range=[180, 180], jitter=True, angle_overhead=self.opt.angle_overhead, angle_front=self.opt.angle_front, uniform_sphere_rate=0)
                        # poses += torch.randn(3, device=rays_o.device, dtype=torch.float)
                        data['dir'] = dirs
                    else:
                        poses, dirs = rand_poses(1, self.device, radius_range=[self.opt.radius_range[0], np.mean(self.opt.radius_range)], return_dirs=self.opt.dir_text, jitter=True, angle_overhead=self.opt.angle_overhead, angle_front=self.opt.angle_front, uniform_sphere_rate=0.5)
                        data['dir'] = dirs
                    fov = data['fov']
                    focal = self.opt.normal_shape / (2 * np.tan(np.deg2rad(fov) / 2))
                    intrinsics = np.array([focal, focal, self.opt.normal_shape / 2, self.opt.normal_shape / 2])
                    rays = get_rays(poses, intrinsics, self.opt.normal_shape, self.opt.normal_shape, -1)
                    rays_o = rays['rays_o'].cuda() # [B, N, 3]
                    rays_d = rays['rays_d'].cuda() # [B, N, 3]

                    H, W = self.opt.normal_shape, self.opt.normal_shape
                    # shading is on
                    l_a = torch.zeros(3, device=rays_o.device, dtype=torch.float) + 0.1
                    l_p = torch.zeros(3, device=rays_o.device, dtype=torch.float) + 0.9
                    if random.random() > self.opt.p_textureless:
                        shading = 'lambertian_df'
                        ambient_ratio = random.random() * 0.6 + 0.1
                    else:
                        shading = 'textureless'
                        ambient_ratio = 0


        if self.front_view:
            bg_color = None
        else:
            bg_color = torch.rand((B * N, 3), device=rays_o.device) # pixel-wise random
        # original light_d is None
        light_d = None
        outputs = self.model.render(rays_o, rays_d, staged=False, perturb=True, bg_color=bg_color, ambient_ratio=ambient_ratio, shading=shading, force_all_rays=True, light_d=light_d, l_a=l_a, l_p=l_p, **vars(self.opt))
        bg_color = torch.rand((B * N, 3), device=rays_o.device) # pixel-wise random

        pred_rgb = outputs['image'].reshape(B, H, W, 3).permute(0, 3, 1, 2).contiguous() # [1, 3, H, W]
        pred_depth = outputs['depth'].reshape(B, H, W, 1).permute(0, 3, 1, 2).contiguous()

        # text embeddings
        if self.opt.dir_text:
            if self.front_view:
                dirs = 0
            else:
                dirs = data['dir'] # [B,]
            text_z = self.text_z[dirs]
            text_z_clip = self.text_z_clip[dirs]
        else:
            text_z = self.text_z
            text_z_clip = self.text_z_clip
        
        loss = 0
        ww = {}

        # occupancy loss
        pred_ws = outputs['weights_sum'].reshape(B, 1, H, W)

        if (np.random.random() < self.opt.p_randbg and shading != 'textureless'):
            # use rand bg
            bg_color = torch.ones_like(pred_rgb) * (torch.rand((B, 3, 1, 1), device=rays_o.device) * 0.6 + 0.2)
            pred_rgb = pred_rgb * pred_ws + bg_color * (1 - pred_ws)
        
        image_ref_clip = self.rgb_clip_embed

        if self.epoch > self.opt.warmup_epoch:
            pred_rgb_sd = pred_rgb
            if self.front_view:
                sd = self.guidance.train_step(self.text_z[0], pred_rgb_sd, image_ref_clip=image_ref_clip, get_clip_img_embedding=self.clip.get_img_embeds, text_z_clip=self.text_z_clip[0], density=pred_ws, clip_guidance_scale=100 if shading != 'textureless' else 0)
                # sd = 0
            else:
                if data['dir'] == 0:
                    # front
                    sd = self.guidance.train_step(text_z, pred_rgb_sd, image_ref_clip=image_ref_clip, get_clip_img_embedding=self.clip.get_img_embeds, text_z_clip=text_z_clip, density=pred_ws, clip_guidance_scale=100 if shading != 'textureless' else 0)
                elif data['dir'] == 2:
                    # back
                    sd = self.guidance.train_step(text_z, pred_rgb_sd, image_ref_clip=image_ref_clip, get_clip_img_embedding=self.clip.get_img_embeds, text_z_clip=text_z_clip, clip_guidance_scale=50 if shading != 'textureless' else 0, guidance_scale=100, density=pred_ws)
                else:
                    # side
                    sd = self.guidance.train_step(text_z, pred_rgb_sd, image_ref_clip=image_ref_clip, get_clip_img_embedding=self.clip.get_img_embeds, text_z_clip=text_z_clip, clip_guidance_scale=50 if shading != 'textureless' else 0, guidance_scale=100, density=pred_ws)
            ww['clip'] = sd['clip'].item()
            ww['sds'] = sd['sds'].item()
            ww['sjc'] = sd['sjc'].item()
            if self.opt.lambda_opacity > 0:
                loss_opacity = (pred_ws ** 2).mean()
                ww['opacity'] = loss_opacity.item()
                if loss_opacity >= 0.5:
                    loss = loss + self.opt.lambda_opacity * 10 * loss_opacity
                else:
                    loss = loss + self.opt.lambda_opacity * loss_opacity

            if self.opt.lambda_entropy > 0:
                alphas = (pred_ws).clamp(1e-5, 1 - 1e-5)
                # alphas = alphas ** 2 # skewed entropy, favors 0 over 1
                loss_entropy = (- alphas * torch.log2(alphas) - (1 - alphas) * torch.log2(1 - alphas)).mean()
                ww['entropy'] = loss_entropy.item()
                        
                loss = loss + self.opt.lambda_entropy * loss_entropy

            if self.opt.lambda_orient > 0 and 'loss_orient' in outputs:
                loss_orient = outputs['loss_orient']
                ww['orient'] = loss_orient.item()
                if self.global_step < 3000:
                    orient_weight = self.global_step / 3000 * (self.opt.lambda_orient - 1e-4) + 1e-4
                else:
                    orient_weight = self.opt.lambda_orient      
                if loss_orient.item() > 1e-2:
                    orient_weight *= 5
                loss = loss + orient_weight * loss_orient

            if self.opt.lambda_smooth > 0 and 'loss_smooth' in outputs:
                loss_smooth = outputs['loss_smooth']
                ww['smooth'] = loss_smooth.item()
                loss = loss + self.opt.lambda_smooth * loss_smooth

            if self.opt.lambda_blur > 0 and 'normals' in outputs:
                normals = outputs['normals'].reshape(B, 3, self.opt.normal_shape, self.opt.normal_shape)
                with torch.no_grad():
                    normals_blur = gaussian_blur2d(normals, (9, 9), (3, 3))
                loss_blur = (normals - normals_blur).square().mean()
                ww['normals_blur'] = loss_blur.item()
                loss = loss + self.opt.lambda_blur * loss_blur

        if self.front_view:
            if self.epoch <= self.opt.warmup_epoch:
                mask_ws = outputs['mask'].reshape(B, H, W) # near < far
                l_rgb = torch.mean(torch.abs(pred_rgb * self.fg_mask_2d - self.rgb * self.fg_mask_2d))
                l_depth = self.margin_rank_loss((pred_depth * self.fg_mask_2d).squeeze()) * 10
                l_ssim = 0
            else:
                l_rgb = torch.mean(torch.abs(pred_rgb * self.fg_mask_2d - self.rgb * self.fg_mask_2d))
                l_depth = self.margin_rank_loss((pred_depth * self.fg_mask_2d).squeeze()) * 10
            l_density = torch.sum(pred_ws * (1 - self.fg_mask_2d)) / torch.sum(1 - self.fg_mask_2d)
            if l_density > 0.05:
                l_density = l_density * 10
            ww['density'] = l_density.item()
            if not isinstance(l_rgb, int):
                ww['front_rgb'] = l_rgb.item()
            # if not isinstance(l_ssim, int):
            #     ww['front_ssim'] = l_ssim.item()
            if not isinstance(l_depth, int):
                ww['front_depth'] = l_depth.item()
            loss = loss + l_rgb * self.opt.ref_rgb_weight + l_depth + l_density
        pred_rgb_aug = pred_rgb
        # CLIP loss
        if self.epoch > self.opt.warmup_epoch and (not self.front_view):
            l_clip = 0

            if data['dir'] == 0:
                l_clip_img = self.clip.img_loss(self.rgb_clip_embed, pred_rgb_aug)
                ww['clip_img'] = l_clip_img.item()
                # front, use CLIP loss
                l_clip = l_clip + l_clip_img * self.opt.clip_img_weight
        else:
            l_clip = 0
            


        if self.global_step % 10 == 0:
            pred_depth = outputs['depth'].reshape(B, H, W, 1).permute(0, 3, 1, 2).contiguous()
            with torch.no_grad():
                im = pred_rgb
                self.writer.add_image('train/img', im[0], self.global_step)
                if self.front_view:
                    self.writer.add_image('train/front_img', im[0], self.global_step)
                depth = pred_depth.squeeze()
                depth = visualize_depth(depth)
                if self.front_view:
                    self.writer.add_image('train/front_depth', depth, self.global_step)
                self.writer.add_image('train/depth', depth, self.global_step)

        idx = outputs['weights_sum'] > 1e-4
        loss_dist = eff_distloss(outputs['weights'][idx], outputs['midpoint'][idx], outputs['deltas'][idx])
        ww['distortion'] = loss_dist.item()
        # if data['dir'] == 0:
        if self.front_view:
            loss_depth_smooth = inverse_depth_smoothness_loss(pred_depth, pred_rgb_aug.float()) * self.opt.front_dsmooth_amplify
        else:
            loss_depth_smooth = inverse_depth_smoothness_loss(pred_depth, pred_rgb_aug.float())
        ww['depth_smooth'] = loss_depth_smooth.item()
        loss = loss + l_clip + loss_depth_smooth
        if self.epoch <= self.opt.warmup_epoch:
            # pass
            loss = loss + loss_dist * self.opt.distortion * self.opt.front_dist_amplify
        elif self.front_view:
            loss = loss + loss_dist * self.opt.distortion * self.opt.front_dist_amplify
        elif data['dir'] == 0 or data['dir'] == 2:
            loss = loss + loss_dist * self.opt.distortion
        else:
            loss = loss + loss_dist * self.opt.distortion

        return pred_rgb, ww, loss

    def post_train_step(self):

        if self.opt.backbone == 'grid':

            lambda_tv = min(1.0, self.global_step / 1000) * self.opt.lambda_tv
            # unscale grad before modifying it!
            # ref: https://pytorch.org/docs/stable/notes/amp_examples.html#gradient-clipping
            self.scaler.unscale_(self.optimizer)
            self.model.encoder.grad_total_variation(lambda_tv, None, self.model.bound)

    def eval_step(self, data):

        rays_o = data['rays_o'] # [B, N, 3]
        rays_d = data['rays_d'] # [B, N, 3]

        B, N = rays_o.shape[:2]
        H, W = data['H'], data['W']

        shading = data['shading'] if 'shading' in data else 'albedo'
        ambient_ratio = data['ambient_ratio'] if 'ambient_ratio' in data else 1.0
        light_d = data['light_d'] if 'light_d' in data else None

        outputs = self.model.render(rays_o, rays_d, staged=True, perturb=False, bg_color=None, light_d=light_d, ambient_ratio=ambient_ratio, shading=shading, force_all_rays=True, **vars(self.opt))
        pred_rgb = outputs['image'].reshape(B, H, W, 3)
        pred_depth = outputs['depth'].reshape(B, H, W)

        # loss = self.opt.lambda_entropy * loss_entropy
        loss = torch.zeros([1], device=pred_rgb.device, dtype=pred_rgb.dtype)


        return pred_rgb, pred_depth, loss

    def test_step(self, data, bg_color=None, perturb=False):  
        rays_o = data['rays_o'] # [B, N, 3]
        rays_d = data['rays_d'] # [B, N, 3]

        B, N = rays_o.shape[:2]
        H, W = data['H'], data['W']

        if bg_color is not None:
            bg_color = bg_color.to(rays_o.device)
        else:
            bg_color = torch.ones(3, device=rays_o.device) # [3]

        shading = data['shading'] if 'shading' in data else 'albedo'
        ambient_ratio = data['ambient_ratio'] if 'ambient_ratio' in data else 1.0
        light_d = data['light_d'] if 'light_d' in data else None

        outputs = self.model.render(rays_o, rays_d, staged=True, perturb=perturb, light_d=light_d, ambient_ratio=ambient_ratio, shading=shading, force_all_rays=True, bg_color=bg_color, **vars(self.opt))

        pred_rgb = outputs['image'].reshape(B, H, W, 3)
        pred_depth = outputs['depth'].reshape(B, H, W)
        pred_mask = outputs['weights_sum'].reshape(B, H, W) > 0.95

        return pred_rgb, pred_depth, pred_mask


    def save_mesh(self, save_path=None, resolution=128):

        if save_path is None:
            save_path = os.path.join(self.workspace, 'mesh')

        self.log(f"==> Saving mesh to {save_path}")

        os.makedirs(save_path, exist_ok=True)

        self.model.export_mesh(save_path, resolution=resolution)

        self.log(f"==> Finished saving mesh.")

    ### ------------------------------

    def train(self, train_loader, valid_loader, max_epochs):

        assert self.text_z is not None, 'Training must provide a text prompt!'

        if self.use_tensorboardX and self.local_rank == 0:
            self.writer = tensorboardX.SummaryWriter(os.path.join(self.workspace, "run", self.name))

        start_t = time.time()
        
        for epoch in range(self.epoch + 1, max_epochs + 1):
            self.epoch = epoch
            self.guidance.set_epoch(epoch)

            self.train_one_epoch(train_loader)

            if self.workspace is not None and self.local_rank == 0:
                self.save_checkpoint(full=True, best=False)

            if self.epoch % self.eval_interval == 0:
                self.evaluate_one_epoch(valid_loader)
                self.save_checkpoint(full=False, best=True)

        end_t = time.time()

        self.log(f"[INFO] training takes {(end_t - start_t)/ 60:.4f} minutes.")

        if self.use_tensorboardX and self.local_rank == 0:
            self.writer.close()

    def evaluate(self, loader, name=None):
        self.use_tensorboardX, use_tensorboardX = False, self.use_tensorboardX
        self.evaluate_one_epoch(loader, name)
        self.use_tensorboardX = use_tensorboardX

    def test(self, loader, save_path=None, name=None, write_video=True):

        if save_path is None:
            save_path = os.path.join(self.workspace, 'results')

        if name is None:
            name = f'{self.name}_ep{self.epoch:04d}'

        os.makedirs(save_path, exist_ok=True)
        
        self.log(f"==> Start Test, save results to {save_path}")

        pbar = tqdm.tqdm(total=len(loader) * loader.batch_size, bar_format='{percentage:3.0f}% {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]')
        self.model.eval()

        if write_video:
            all_preds = []
            all_preds_depth = []

        with torch.no_grad():

            for i, data in enumerate(loader):
                
                with torch.cuda.amp.autocast(enabled=self.fp16):
                    preds, preds_depth, preds_mask = self.test_step(data)

                pred = preds[0].detach().cpu().numpy()
                pred = (pred * 255).astype(np.uint8)

                # pred_depth = preds_depth[0].detach().cpu().numpy()
                pred_depth = visualize_depth(preds_depth[0])
                pred_depth = (pred_depth * 255).cpu().permute(1, 2, 0).numpy().astype(np.uint8)

                if write_video:
                    all_preds.append(pred)
                    all_preds_depth.append(pred_depth)
                else:
                    cv2.imwrite(os.path.join(save_path, f'{name}_{i:04d}_rgb.png'), cv2.cvtColor(pred, cv2.COLOR_RGB2BGR))
                    cv2.imwrite(os.path.join(save_path, f'{name}_{i:04d}_depth.png'), pred_depth)

                pbar.update(loader.batch_size)

        if write_video:
            all_preds = np.stack(all_preds, axis=0)
            all_preds_depth = np.stack(all_preds_depth, axis=0)
            
            imageio.mimwrite(os.path.join(save_path, f'{name}_rgb.mp4'), all_preds, fps=25, quality=8, macro_block_size=1)
            imageio.mimwrite(os.path.join(save_path, f'{name}_depth.mp4'), all_preds_depth, fps=25, quality=8, macro_block_size=1)

        self.log(f"==> Finished Test.")
    
    # [GUI] train text step.
    def train_gui(self, train_loader, epoch, step=100):

        self.model.train()
        self.epoch = epoch
        self.guidance.set_epoch(epoch)

        total_loss = torch.tensor([0], dtype=torch.float32, device=self.device)
        
        loader = iter(train_loader)

        for _ in range(step):
            
            # mimic an infinite loop dataloader (in case the total dataset is smaller than step)
            try:
                data = next(loader)
            except StopIteration:
                loader = iter(train_loader)
                data = next(loader)

            # update grid every 16 steps
            if self.model.cuda_ray and self.global_step % self.opt.update_extra_interval == 0:
                with torch.cuda.amp.autocast(enabled=self.fp16):
                    self.model.update_extra_state()
            
            self.global_step += 1

            self.optimizer.zero_grad()

            with torch.cuda.amp.autocast(enabled=self.fp16):
                pred_rgbs, pred_ws, loss = self.train_step(data)
         
            self.scaler.scale(loss).backward()
            self.scaler.step(self.optimizer)
            self.scaler.update()
            
            if self.scheduler_update_every_step:
                self.lr_scheduler.step()

            total_loss += loss.detach()

        if self.ema is not None:
            self.ema.update()

        average_loss = total_loss.item() / step

        if not self.scheduler_update_every_step:
            if isinstance(self.lr_scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
                self.lr_scheduler.step(average_loss)
            else:
                self.lr_scheduler.step()

        outputs = {
            'loss': average_loss,
            'lr': self.optimizer.param_groups[0]['lr'],
        }
        
        return outputs

    
    # [GUI] test on a single image
    def test_gui(self, pose, intrinsics, W, H, bg_color=None, spp=1, downscale=1, light_d=None, ambient_ratio=1.0, shading='albedo'):
        
        # render resolution (may need downscale to for better frame rate)
        rH = int(H * downscale)
        rW = int(W * downscale)
        intrinsics = intrinsics * downscale

        pose = torch.from_numpy(pose).unsqueeze(0).to(self.device)

        rays = get_rays(pose, intrinsics, rH, rW, -1)

        # from degree theta/phi to 3D normalized vec
        light_d = np.deg2rad(light_d)
        light_d = np.array([
            np.sin(light_d[0]) * np.sin(light_d[1]),
            np.cos(light_d[0]),
            np.sin(light_d[0]) * np.cos(light_d[1]),
        ], dtype=np.float32)
        light_d = torch.from_numpy(light_d).to(self.device)

        data = {
            'rays_o': rays['rays_o'],
            'rays_d': rays['rays_d'],
            'H': rH,
            'W': rW,
            'light_d': light_d,
            'ambient_ratio': ambient_ratio,
            'shading': shading,
        }
        
        self.model.eval()

        if self.ema is not None:
            self.ema.store()
            self.ema.copy_to()

        with torch.no_grad():
            with torch.cuda.amp.autocast(enabled=self.fp16):
                # here spp is used as perturb random seed!
                preds, preds_depth = self.test_step(data, bg_color=bg_color, perturb=spp)

        if self.ema is not None:
            self.ema.restore()

        # interpolation to the original resolution
        if downscale != 1:
            # have to permute twice with torch...
            preds = F.interpolate(preds.permute(0, 3, 1, 2), size=(H, W), mode='nearest').permute(0, 2, 3, 1).contiguous()
            preds_depth = F.interpolate(preds_depth.unsqueeze(1), size=(H, W), mode='nearest').squeeze(1)

        outputs = {
            'image': preds[0].detach().cpu().numpy(),
            'depth': preds_depth[0].detach().cpu().numpy(),
        }

        return outputs

    def train_one_epoch(self, loader):
        self.log(f"==> Start Training {self.workspace} Epoch {self.epoch}, lr={self.optimizer.param_groups[0]['lr']:.6f} ...")

        total_loss = 0
        if self.local_rank == 0 and self.report_metric_at_train:
            for metric in self.metrics:
                metric.clear()

        self.model.train()

        # distributedSampler: must call set_epoch() to shuffle indices across multiple epochs
        # ref: https://pytorch.org/docs/stable/data.html
        if self.world_size > 1:
            loader.sampler.set_epoch(self.epoch)
        
        if self.local_rank == 0:
            pbar = tqdm.tqdm(total=len(loader) * loader.batch_size, bar_format='{desc}: {percentage:3.0f}% {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]')

        self.local_step = 0

        for data in loader:
            
            # update grid every 16 steps
            if self.model.cuda_ray and self.global_step % self.opt.update_extra_interval == 0:
                with torch.cuda.amp.autocast(enabled=self.fp16):
                    self.model.update_extra_state()
                    
            self.local_step += 1
            self.global_step += 1

            self.optimizer.zero_grad()

            with torch.cuda.amp.autocast(enabled=self.fp16):
                pred_rgbs, ww, loss = self.train_step(data)
         
            self.scaler.scale(loss).backward()
            self.scaler.step(self.optimizer)
            self.scaler.update()

            if self.scheduler_update_every_step:
                self.lr_scheduler.step()

            loss_val = loss.item()
            total_loss += loss_val

            if self.local_rank == 0:
                if self.use_tensorboardX:
                    self.writer.add_scalar("train/loss", loss_val, self.global_step)
                    self.writer.add_scalar("train/lr", self.optimizer.param_groups[0]['lr'], self.global_step)
                    for k, v in ww.items():
                        if k == 'tot':
                            continue
                        if k == 'sd_component':
                            continue
                        self.writer.add_scalar(f"train/loss_{k}", v, self.global_step)

                if self.scheduler_update_every_step:
                    pbar.set_description(f"loss={loss_val:.4f} ({total_loss/self.local_step:.4f}), lr={self.optimizer.param_groups[0]['lr']:.6f}")
                else:
                    pbar.set_description(f"loss={loss_val:.4f} ({total_loss/self.local_step:.4f})")
                pbar.update(loader.batch_size)

        if self.ema is not None:
            self.ema.update()

        average_loss = total_loss / self.local_step
        self.stats["loss"].append(average_loss)

        if self.local_rank == 0:
            pbar.close()
            if self.report_metric_at_train:
                for metric in self.metrics:
                    self.log(metric.report(), style="red")
                    if self.use_tensorboardX:
                        metric.write(self.writer, self.epoch, prefix="train")
                    metric.clear()

        if not self.scheduler_update_every_step:
            if isinstance(self.lr_scheduler, torch.optim.lr_scheduler.ReduceLROnPlateau):
                self.lr_scheduler.step(average_loss)
            else:
                self.lr_scheduler.step()

        self.log(f"==> Finished Epoch {self.epoch}.")


    def evaluate_one_epoch(self, loader, name=None):
        self.log(f"++> Evaluate {self.workspace} at epoch {self.epoch} ...")

        if name is None:
            name = f'{self.name}_ep{self.epoch:04d}'

        total_loss = 0
        if self.local_rank == 0:
            for metric in self.metrics:
                metric.clear()

        self.model.eval()

        if self.ema is not None:
            self.ema.store()
            self.ema.copy_to()

        if self.local_rank == 0:
            pbar = tqdm.tqdm(total=len(loader) * loader.batch_size, bar_format='{desc}: {percentage:3.0f}% {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]')

        with torch.no_grad():
            self.local_step = 0

            for data in loader:    
                self.local_step += 1

                with torch.cuda.amp.autocast(enabled=self.fp16):
                    preds, preds_depth, loss = self.eval_step(data)

                # all_gather/reduce the statistics (NCCL only support all_*)
                if self.world_size > 1:
                    dist.all_reduce(loss, op=dist.ReduceOp.SUM)
                    loss = loss / self.world_size
                    
                    preds_list = [torch.zeros_like(preds).to(self.device) for _ in range(self.world_size)] # [[B, ...], [B, ...], ...]
                    dist.all_gather(preds_list, preds)
                    preds = torch.cat(preds_list, dim=0)

                    preds_depth_list = [torch.zeros_like(preds_depth).to(self.device) for _ in range(self.world_size)] # [[B, ...], [B, ...], ...]
                    dist.all_gather(preds_depth_list, preds_depth)
                    preds_depth = torch.cat(preds_depth_list, dim=0)
                
                loss_val = loss.item()
                total_loss += loss_val

                # only rank = 0 will perform evaluation.
                if self.local_rank == 0:

                    # save image
                    save_path = os.path.join(self.workspace, 'validation', f'{name}_{self.local_step:04d}_rgb.png')
                    save_path_depth = os.path.join(self.workspace, 'validation', f'{name}_{self.local_step:04d}_depth.png')

                    #self.log(f"==> Saving validation image to {save_path}")
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)

                    pred = preds[0].detach().cpu().numpy()
                    pred = (pred * 255).astype(np.uint8)

                    pred_depth = preds_depth[0].detach().cpu().numpy()
                    pred_depth = (pred_depth * 255).astype(np.uint8)
                    
                    cv2.imwrite(save_path, cv2.cvtColor(pred, cv2.COLOR_RGB2BGR))
                    cv2.imwrite(save_path_depth, pred_depth)

                    pbar.set_description(f"loss={loss_val:.4f} ({total_loss/self.local_step:.4f})")
                    pbar.update(loader.batch_size)


        average_loss = total_loss / self.local_step
        self.stats["valid_loss"].append(average_loss)

        if self.local_rank == 0:
            pbar.close()
            if not self.use_loss_as_metric and len(self.metrics) > 0:
                result = self.metrics[0].measure()
                self.stats["results"].append(result if self.best_mode == 'min' else - result) # if max mode, use -result
            else:
                self.stats["results"].append(average_loss) # if no metric, choose best by min loss

            for metric in self.metrics:
                self.log(metric.report(), style="blue")
                if self.use_tensorboardX:
                    metric.write(self.writer, self.epoch, prefix="evaluate")
                metric.clear()

        if self.ema is not None:
            self.ema.restore()

        self.log(f"++> Evaluate epoch {self.epoch} Finished.")

    def save_checkpoint(self, name=None, full=False, best=False):

        if name is None:
            name = f'{self.name}_ep{self.epoch:04d}'

        state = {
            'epoch': self.epoch,
            'global_step': self.global_step,
            'stats': self.stats,
        }

        if self.model.cuda_ray:
            state['mean_count'] = self.model.mean_count
            state['mean_density'] = self.model.mean_density

        if full:
            state['optimizer'] = self.optimizer.state_dict()
            state['lr_scheduler'] = self.lr_scheduler.state_dict()
            state['scaler'] = self.scaler.state_dict()
            if self.ema is not None:
                state['ema'] = self.ema.state_dict()
        
        if not best:

            state['model'] = self.model.state_dict()

            file_path = f"{name}.pth"

            self.stats["checkpoints"].append(file_path)

            if len(self.stats["checkpoints"]) > self.max_keep_ckpt:
                old_ckpt = os.path.join(self.ckpt_path, self.stats["checkpoints"].pop(0))
                if os.path.exists(old_ckpt):
                    os.remove(old_ckpt)

            torch.save(state, os.path.join(self.ckpt_path, file_path))

        else:    
            if len(self.stats["results"]) > 0:
                if self.stats["best_result"] is None or self.stats["results"][-1] < self.stats["best_result"]:
                    self.log(f"[INFO] New best result: {self.stats['best_result']} --> {self.stats['results'][-1]}")
                    self.stats["best_result"] = self.stats["results"][-1]

                    # save ema results 
                    if self.ema is not None:
                        self.ema.store()
                        self.ema.copy_to()

                    state['model'] = self.model.state_dict()

                    if self.ema is not None:
                        self.ema.restore()
                    
                    torch.save(state, self.best_path)
            else:
                self.log(f"[WARN] no evaluated results found, skip saving best checkpoint.")
            
    def load_checkpoint(self, checkpoint=None, model_only=False):
        if checkpoint is None:
            checkpoint_list = sorted(glob.glob(f'{self.ckpt_path}/*.pth'))
            if checkpoint_list:
                checkpoint = checkpoint_list[-1]
                self.log(f"[INFO] Latest checkpoint is {checkpoint}")
            else:
                self.log("[WARN] No checkpoint found, model randomly initialized.")
                return

        checkpoint_dict = torch.load(checkpoint, map_location=self.device)
        
        if 'model' not in checkpoint_dict:
            self.model.load_state_dict(checkpoint_dict)
            self.log("[INFO] loaded model.")
            return

        missing_keys, unexpected_keys = self.model.load_state_dict(checkpoint_dict['model'], strict=False)
        self.log("[INFO] loaded model.")
        if len(missing_keys) > 0:
            self.log(f"[WARN] missing keys: {missing_keys}")
        if len(unexpected_keys) > 0:
            self.log(f"[WARN] unexpected keys: {unexpected_keys}")   

        if self.ema is not None and 'ema' in checkpoint_dict:
            try:
                self.ema.load_state_dict(checkpoint_dict['ema'])
                self.log("[INFO] loaded EMA.")
            except:
                self.log("[WARN] failed to loaded EMA.")

        if self.model.cuda_ray:
            if 'mean_count' in checkpoint_dict:
                self.model.mean_count = checkpoint_dict['mean_count']
            if 'mean_density' in checkpoint_dict:
                self.model.mean_density = checkpoint_dict['mean_density']

        if model_only:
            return

        self.stats = checkpoint_dict['stats']
        self.epoch = checkpoint_dict['epoch']
        self.global_step = checkpoint_dict['global_step']
        self.log(f"[INFO] load at epoch {self.epoch}, global step {self.global_step}")
        
        if self.optimizer and 'optimizer' in checkpoint_dict:
            try:
                self.optimizer.load_state_dict(checkpoint_dict['optimizer'])
                self.log("[INFO] loaded optimizer.")
            except:
                self.log("[WARN] Failed to load optimizer.")
        
        if self.lr_scheduler and 'lr_scheduler' in checkpoint_dict:
            try:
                self.lr_scheduler.load_state_dict(checkpoint_dict['lr_scheduler'])
                self.log("[INFO] loaded scheduler.")
            except:
                self.log("[WARN] Failed to load scheduler.")
        
        if self.scaler and 'scaler' in checkpoint_dict:
            try:
                self.scaler.load_state_dict(checkpoint_dict['scaler'])
                self.log("[INFO] loaded scaler.")
            except:
                self.log("[WARN] Failed to load scaler.")