import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Function
from torch.cuda.amp import custom_fwd, custom_bwd 

try:
    import _gridencoder as _backend
except ImportError:
    from .backend import _backend

# Mapping grid type strings to IDs
_gridtype_to_id = {
    'hash': 0,
    'tiled': 1,
}

class _grid_encode(Function):
    @staticmethod
    @custom_fwd(cast_inputs=torch.float32)
    def forward(ctx, inputs, embeddings, offsets, per_level_scale, base_resolution, calc_grad_inputs=False, gridtype=0, align_corners=False):
        inputs = inputs.contiguous()

        B, D = inputs.shape  # Batch size, coordinate dimension
        L = offsets.shape[0] - 1  # Number of levels
        C = embeddings.shape[1]  # Embedding dimension for each level
        S = np.log2(per_level_scale)  # Resolution multiplier, using log2 for CUDA exp2f
        H = base_resolution  # Base resolution

        # Use half precision for embeddings if in autocast mode and level dimension is even
        if torch.is_autocast_enabled() and C % 2 == 0:
            embeddings = embeddings.to(torch.float16)

        # Outputs have shape [L, B, C]
        outputs = torch.empty(L, B, C, device=inputs.device, dtype=embeddings.dtype)

        if calc_grad_inputs:
            dy_dx = torch.empty(B, L * D * C, device=inputs.device, dtype=embeddings.dtype)
        else:
            dy_dx = None

        # Call CUDA backend function
        _backend.grid_encode_forward(inputs, embeddings, offsets, outputs, B, D, C, L, S, H, dy_dx, gridtype, align_corners)

        # Permute and reshape output to [B, L * C]
        outputs = outputs.permute(1, 0, 2).reshape(B, L * C)

        ctx.save_for_backward(inputs, embeddings, offsets, dy_dx)
        ctx.dims = [B, D, C, L, S, H, gridtype]
        ctx.align_corners = align_corners

        return outputs
    
    @staticmethod
    @custom_bwd
    def backward(ctx, grad):
        inputs, embeddings, offsets, dy_dx = ctx.saved_tensors
        B, D, C, L, S, H, gridtype = ctx.dims
        align_corners = ctx.align_corners

        # Reshape gradient to [L, B, C] for the backward pass
        grad = grad.view(B, L, C).permute(1, 0, 2).contiguous()

        grad_embeddings = torch.zeros_like(embeddings)

        if dy_dx is not None:
            grad_inputs = torch.zeros_like(inputs, dtype=embeddings.dtype)
        else:
            grad_inputs = None

        # Call CUDA backend function for backward pass
        _backend.grid_encode_backward(grad, inputs, embeddings, offsets, grad_embeddings, B, D, C, L, S, H, dy_dx, grad_inputs, gridtype, align_corners)

        if dy_dx is not None:
            grad_inputs = grad_inputs.to(inputs.dtype)

        return grad_inputs, grad_embeddings, None, None, None, None, None, None


grid_encode = _grid_encode.apply


class GridEncoder(nn.Module):
    def __init__(self, input_dim=3, num_levels=16, level_dim=2, per_level_scale=2.0, base_resolution=16, log2_hashmap_size=19, desired_resolution=None, gridtype='hash', align_corners=False):
        super().__init__()

        if desired_resolution is not None:
            per_level_scale = np.exp2(np.log2(desired_resolution / base_resolution) / (num_levels - 1))

        self.input_dim = input_dim
        self.num_levels = num_levels
        self.level_dim = level_dim
        self.per_level_scale = per_level_scale
        self.log2_hashmap_size = log2_hashmap_size
        self.base_resolution = base_resolution
        self.output_dim = num_levels * level_dim
        self.gridtype = gridtype
        self.gridtype_id = _gridtype_to_id[gridtype]
        self.align_corners = align_corners

        # Calculate offsets for each level
        offsets = []
        offset = 0
        self.max_params = 2 ** log2_hashmap_size
        for i in range(num_levels):
            resolution = int(np.ceil(base_resolution * per_level_scale ** i))
            params_in_level = min(self.max_params, (resolution if align_corners else resolution + 1) ** input_dim)
            params_in_level = int(np.ceil(params_in_level / 8) * 8)
            offsets.append(offset)
            offset += params_in_level
        offsets.append(offset)
        offsets = torch.from_numpy(np.array(offsets, dtype=np.int32))
        self.register_buffer('offsets', offsets)

        self.n_params = offsets[-1] * level_dim

        # Define the embeddings parameter
        self.embeddings = nn.Parameter(torch.empty(offset, level_dim))
        self.reset_parameters()
    
    def reset_parameters(self):
        """Initialize embeddings with a small random value."""
        std = 1e-4
        self.embeddings.data.uniform_(-std, std)

    def __repr__(self):
        return (f"GridEncoder: input_dim={self.input_dim}, num_levels={self.num_levels}, "
                f"level_dim={self.level_dim}, resolution={self.base_resolution} -> "
                f"{int(round(self.base_resolution * self.per_level_scale ** (self.num_levels - 1)))}, "
                f"per_level_scale={self.per_level_scale:.4f}, params={tuple(self.embeddings.shape)}, "
                f"gridtype={self.gridtype}, align_corners={self.align_corners}")
    
    def forward(self, inputs, bound=1):
        inputs = (inputs + bound) / (2 * bound)

        prefix_shape = list(inputs.shape[:-1])
        inputs = inputs.view(-1, self.input_dim)

        outputs = grid_encode(inputs, self.embeddings, self.offsets, self.per_level_scale, self.base_resolution, inputs.requires_grad, self.gridtype_id, self.align_corners)
        outputs = outputs.view(prefix_shape + [self.output_dim])

        return outputs
