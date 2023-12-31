import copy
import random
from threading import Lock

from torch import nn
from torch_geometric import nn as geom_nn

from .block import LearnableBlock, compute_out_channels, fix_heads_output_block
from .hypermodel import HyperModel
from .utils import get_heads_from_layer, compute_prev_block_heads, reset_model_parameters, get_concat_from_layer, \
    compute_prev_block_concat, retrieve_layer_config, compute_layer_out_shape


def compute_prev_out_shape(prev_layer):
    prev_out_shape = prev_layer.out_channels
    concat = get_concat_from_layer(prev_layer)
    if concat:
        heads = get_heads_from_layer(prev_layer)
        prev_out_shape *= heads

    return prev_out_shape


class SearchSpace:
    def __init__(self, num_node_features: int, data_out_shape: int, max_depth: int):
        self.data_out_shape = data_out_shape
        self.num_node_features = num_node_features
        self.max_depth = max_depth
        self.space = {1: [LearnableBlock(is_input=True, is_output=True)]}
        self.lock = Lock()

    def get_init_model(self) -> HyperModel:
        return HyperModel([(geom_nn.GraphConv(in_channels=self.num_node_features, out_channels=self.data_out_shape),
                            nn.ReLU(), nn.Dropout(p=0.0))])

    def learn(self, model: HyperModel, positive: bool):
        with self.lock:
            depth = len(model.get_blocks())
            for mod_block, block in zip(model.get_blocks(), self.space[depth]):
                block.learn(layer=mod_block[0], regularization=mod_block[2], activation=mod_block[1], positive=positive)

    def update_previous_state(self, model: HyperModel):
        with self.lock:
            blocks = model.get_blocks()
            depth = len(blocks)
            for learnable_block, model_block in zip(self.space[depth], blocks):
                learnable_block.set_previous_state(model_block)

    def query_for_depth(self, depth: int) -> HyperModel:
        with self.lock:
            model = []
            # If this is the first time the search space has been queried for a model of such depth, initialize it.
            # It is worth noting that the queries must be in ascending order and there cannot be any gaps; that is:
            # If the space has depths 1, 2 and 3, before querying for 5, a query for 4 must happen.
            self._extend_search_space(depth)

            # Iterate over the blocks and query them with the appropriate input and output dimensions
            for block in self.space[depth]:
                prev_out_shape = compute_prev_out_shape(model[-1][0]) if not block.get_input() else self.num_node_features
                gen_block = block.query(prev_out_shape=prev_out_shape, data_out_shape=self.data_out_shape)
                model.append(gen_block)

            return HyperModel(model_blocks=model)

    def _extend_search_space(self, depth: int):
        if depth not in self.space.keys():
            self.space[depth] = copy.deepcopy(self.space[depth - 1])
            self.space[depth][-1].disable_output()
            self.space[depth].append(LearnableBlock(is_output=True))

    def increase_model_depth(self, model: HyperModel) -> HyperModel:
        blocks = model.get_blocks()
        new_depth = len(blocks) + 1

        if self.max_depth and new_depth > self.max_depth:
            return model

        self._extend_search_space(new_depth)

        prev_out_shape = compute_prev_out_shape(blocks[-2][0]) if new_depth > 2 else self.num_node_features
        params, dim_ratio = retrieve_layer_config(blocks[-1][0])
        new_out_channels = compute_out_channels(False, self.data_out_shape, prev_out_shape, dim_ratio, params)

        blocks[-1] = self.space[new_depth - 1][-1].rebuild_block(new_out_channels=new_out_channels)
        current_out_shape = compute_prev_out_shape(blocks[-1][0])
        blocks.append(self.space[new_depth][-1].query(current_out_shape, self.data_out_shape))

        reset_model_parameters(blocks)

        return HyperModel(model_blocks=blocks)

    def reduce_model_depth(self, model: HyperModel) -> HyperModel:
        blocks = model.get_blocks()
        depth = len(blocks)
        blocks.pop()

        params, _ = retrieve_layer_config(blocks[-1][0])
        fix_heads_output_block(is_output=True, sampled_params=params)
        blocks[-1] = self.space[depth][-2].rebuild_block(new_out_channels=self.data_out_shape, new_params=params)

        reset_model_parameters(blocks)

        return HyperModel(model_blocks=blocks)

    def _adjust_next_block(self, new_block, old_block_out_shape: int, model_depth: int, block_idx: int, blocks: list):
        new_block_out_shape = compute_layer_out_shape(new_block[0])

        # If the output shape of the old block differs from the new one, adjust the input of the next block
        if new_block_out_shape != old_block_out_shape and block_idx != model_depth - 1:
            blocks[block_idx + 1] = self.space[model_depth][block_idx + 1].rebuild_block(
                new_in_channels=new_block_out_shape)

    def _compute_new_in_channels(self, block_idx: int, blocks: list) -> int:
        if block_idx == 0:
            return self.num_node_features

        prev_block_concat = compute_prev_block_concat(block_idx, blocks)
        if prev_block_concat:
            prev_block_heads = compute_prev_block_heads(block_idx, blocks)
            return prev_block_heads * blocks[block_idx - 1][0].out_channels

        return blocks[block_idx - 1][0].out_channels

    def query_for_component(self, model: HyperModel, complete_layer: bool) -> HyperModel:
        with self.lock:
            # Get current model's depth and pick a random block to be changed
            blocks = model.get_blocks()
            model_depth = len(blocks)
            block_idx = random.randint(0, model_depth - 1)

            # Retrieve old block output shape in order to check if adjustments are necessary once its updated
            old_block_out_shape = compute_layer_out_shape(blocks[block_idx][0])

            # Compute new input shape, query for a new block and substitute the old one
            new_in_channels = self._compute_new_in_channels(block_idx, blocks)

            if complete_layer:
                new_block = self.space[model_depth][block_idx].query(prev_out_shape=new_in_channels, data_out_shape=self.data_out_shape)
            else:
                prev_block_out_shape = compute_prev_out_shape(blocks[block_idx - 1][0]) if block_idx != 0 else self.num_node_features
                new_block = self.space[model_depth][block_idx].query_hyperparameters_for_block(blocks[block_idx],
                                                                                               self.data_out_shape,
                                                                                               prev_block_out_shape)

            # Update the block
            blocks[block_idx] = new_block

            # Adjust the next block input dimensions if required
            self._adjust_next_block(new_block, old_block_out_shape, model_depth, block_idx, blocks)

            # Reset model parameters to avoid overfitting when training the model again.
            reset_model_parameters(blocks)

            return HyperModel(model_blocks=blocks)
