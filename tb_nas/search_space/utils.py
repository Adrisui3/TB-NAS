from .hyperparameters.default_values import DEFAULT_HYPERPARAMETERS
from .hyperparameters.dimension_ratio import DimensionRatio


def has_heads_parameter(layer):
    return "heads" in layer.__dict__.keys()


def has_concat_parameter(layer) -> bool:
    return "concat" in layer.__dict__.keys()


def reset_model_parameters(model_blocks: list):
    for block in model_blocks:
        block[0].reset_parameters()


def get_heads_from_layer(layer) -> int:
    return layer.heads if has_heads_parameter(layer) else 1


def get_concat_from_layer(layer) -> bool:
    return layer.concat if has_concat_parameter(layer) else False


def compute_prev_block_heads(block_idx: int, blocks: list) -> int:
    if block_idx > 0:
        return get_heads_from_layer(blocks[block_idx - 1][0])

    return 1


def compute_prev_block_concat(block_idx: int, blocks: list) -> bool:
    if block_idx > 0:
        return get_concat_from_layer(blocks[block_idx - 1][0])

    return False


def compute_layer_out_shape(layer):
    block_heads = get_heads_from_layer(layer)
    block_concat = get_concat_from_layer(layer)
    return block_heads * layer.out_channels if block_concat else layer.out_channels


def update_for_cheb_conv(prev_params: dict, layer):
    # This is a workaround used to retrieve parameter K for ChebConv since it's not stored as usual
    if "K" in DEFAULT_HYPERPARAMETERS[layer.__class__.__name__].keys() and "K" not in prev_params:
        prev_params["K"] = len(layer.__dict__["_modules"]["lins"])


def get_module_params(module) -> dict:
    return {key: module.__dict__[key] for key in module.__dict__.keys() if
            key in DEFAULT_HYPERPARAMETERS[module.__class__.__name__].keys()}


def retrieve_layer_config(layer):
    # Given a layer, it retrieves the set of parameters and values which are considered for optimization.
    # It is worth noting that the complete set of parameters of the layer might be bigger.
    prev_params = get_module_params(layer)

    update_for_cheb_conv(prev_params, layer)

    concat_param = prev_params["concat"] if "concat" in prev_params.keys() else False
    heads_param = prev_params["heads"] if "heads" in prev_params.keys() and concat_param else 1
    out_shape = layer.out_channels * heads_param
    if layer.in_channels == out_shape:
        prev_ratio = DimensionRatio.EQUAL
    elif layer.in_channels > out_shape:
        prev_ratio = DimensionRatio.REDUCE
    else:
        prev_ratio = DimensionRatio.INCREASE

    return prev_params, prev_ratio
