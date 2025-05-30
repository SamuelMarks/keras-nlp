import keras
from keras import ops

from keras_hub.src.api_export import keras_hub_export
from keras_hub.src.models.gemma.rms_normalization import RMSNormalization
from keras_hub.src.utils.keras_utils import clone_initializer


@keras_hub_export("keras_hub.models.Gemma3VisionEncoder")
class Gemma3VisionEncoder(keras.Model):
    """Vision Transformer (ViT) model for Gemma3.

    Args:
        image_size: int. The height/width of the image. Both height and width is
            expected to be the same.
        patch_size: int. The size of each square patch in the input image.
        num_heads: int. The number of attention heads for the vision(image)
            transformer encoder.
        hidden_dim: int. The size of the transformer hidden state at the end
            of each vision transformer layer.
        num_layers: int. The number of transformer layers.
        intermediate_dim: int. The output dimension of the first Dense layer in
            a two-layer feedforward network for transformer.
        output_dim: int. The odimension of the output returned by the model.
        pool_size: int. Factors by which to downscale `(dim1, dim2)` in the
            average pooling layer. The same value is used for `"strides"`.
            Defaults to 14.
        layer_norm_epsilon: float. The epsilon value user for every layer norm
            in all transformer blocks. Defaults to `1e-6`.
        dtype: string or `keras.mixed_precision.DTypePolicy`. The dtype to use
            for the models computations and weights. Note that some
            computations, such as softmax and layer normalization will always
            be done a float32 precision regardless of dtype.

    Example:
    ```python
    image = np.random.rand(224, 224, 3)
    vit_model = Gemma3VisionEncoder(image_size=224)
    # The output will be of shape:
    # [batch_size, num_vision_tokens_per_image, hidden_dim]
    output = vit_model([image])
    ```
    """

    def __init__(
        self,
        image_size,
        patch_size,
        num_heads,
        hidden_dim,
        num_layers,
        intermediate_dim,
        output_dim,
        pool_size=14,
        layer_norm_epsilon=1e-6,
        dtype=None,
        **kwargs,
    ):
        # If the passed dtype is `bfloat16`, use `float32` to maintain parity
        # with other framework implementations.
        if dtype == "bfloat16":
            dtype = "float32"

        # === Functional Model ===
        image_input = keras.Input(
            shape=(None, image_size, image_size, 3),
            name="images",
        )
        x = image_input  # Intermediate result.
        x = Gemma3VisionEncoderBlock(
            hidden_dim=hidden_dim,
            num_layers=num_layers,
            num_heads=num_heads,
            intermediate_dim=intermediate_dim,
            patch_size=patch_size,
            image_size=image_size,
            dtype=dtype,
            name="image_encoder",
        )(x)

        x = Gemma3VisionAveragePooling(
            image_size=image_size,
            patch_size=patch_size,
            pool_size=pool_size,
            dtype=dtype,
            name="pooling",
        )(x)

        x = Gemma3VisionOutput(
            output_dim=output_dim,
            layer_norm_epsilon=layer_norm_epsilon,
            kernel_initializer=keras.initializers.RandomNormal(
                mean=0.0, stddev=0.01
            ),
            dtype=dtype,
            name="vision_output_encoder",
        )(x)

        outputs = x
        super().__init__(
            inputs=image_input,
            outputs=outputs,
            **kwargs,
        )

        # === Config ===
        self.image_size = image_size
        self.patch_size = patch_size
        self.num_heads = num_heads
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers
        self.intermediate_dim = intermediate_dim
        self.output_dim = output_dim
        self.pool_size = pool_size
        self.layer_norm_epsilon = layer_norm_epsilon
        self.num_vision_tokens_per_image = (
            (image_size // patch_size) ** 2
        ) // (pool_size**2)

        # Before Keras 3.2, there is no `keras.dtype_policies.get`.
        if hasattr(keras.dtype_policies, "get"):
            self.dtype_policy = keras.dtype_policies.get(dtype)
        else:
            if isinstance(dtype, keras.dtype_policies.DTypePolicy):
                dtype = dtype.name
            dtype = dtype or keras.config.dtype_policy().name
            self.dtype_policy = keras.dtype_policies.DTypePolicy(dtype)

    def get_config(self):
        config = super().get_config()
        config.update(
            {
                "num_heads": self.num_heads,
                "hidden_dim": self.hidden_dim,
                "num_layers": self.num_layers,
                "intermediate_dim": self.intermediate_dim,
                "output_dim": self.output_dim,
                "pool_size": self.pool_size,
                "image_size": self.image_size,
                "patch_size": self.patch_size,
                "layer_norm_epsilon": self.layer_norm_epsilon,
            }
        )
        return config


class Gemma3VisionEmbedding(keras.layers.Layer):
    def __init__(
        self,
        image_size,
        patch_size,
        hidden_dim,
        num_channels=3,
        dtype=None,
        **kwargs,
    ):
        super().__init__(dtype=dtype, **kwargs)
        self.hidden_dim = hidden_dim
        self.image_size = image_size
        self.patch_size = patch_size
        self.num_channels = num_channels
        self.patch_embedding = keras.layers.Conv2D(
            filters=self.hidden_dim,
            kernel_size=self.patch_size,
            strides=self.patch_size,
            padding="valid",
            activation=None,
            dtype=dtype,
            name="embedding_conv",
        )
        self.num_patches = (self.image_size // self.patch_size) ** 2
        self.num_positions = self.num_patches
        self.position_embedding = keras.layers.Embedding(
            self.num_positions,
            self.hidden_dim,
            dtype=dtype,
            name="position_embedding",
        )

        self.position_ids = ops.expand_dims(
            ops.arange(self.num_positions), axis=0
        )

    def build(self, input_shape):
        self.patch_embedding.build(input_shape)
        self.position_embedding.build([1, self.num_positions])
        self.built = True

    def call(self, input_tokens):
        x = self.patch_embedding(input_tokens)
        input_shape = ops.shape(x)
        x = ops.reshape(x, [input_shape[0], self.num_patches, self.hidden_dim])
        x = x + self.position_embedding(self.position_ids)
        return x

    def compute_output_shape(self, input_shape):
        return (
            input_shape[0],
            self.num_patches,
            self.hidden_dim,
        )


class Gemma3VisionAttention(keras.layers.Layer):
    """
    Adapted from https://github.com/huggingface/transformers/blob/main/src/transformers/models/clip/modeling_clip.py
    """

    def __init__(
        self,
        hidden_dim,
        num_heads,
        dropout=0.0,
        dtype=None,
        **kwargs,
    ):
        super().__init__(dtype=dtype, **kwargs)

        self.hidden_dim = hidden_dim
        self.num_heads = num_heads
        self.dropout = dropout
        self.head_dim = self.hidden_dim // self.num_heads
        if self.head_dim * self.num_heads != self.hidden_dim:
            raise ValueError(
                f"hidden_dim must be divisible by num_heads (got `hidden_dim`"
                f": {self.hidden_dim} and `num_heads`:"
                f" {self.num_heads})."
            )
        self.dropout_layer = keras.layers.Dropout(
            self.dropout,
            dtype=dtype,
            name="dropout",
        )
        self.scale = self.head_dim**-0.5
        self.query_proj = keras.layers.Dense(
            units=self.hidden_dim,
            dtype=dtype,
            name="query_proj",
        )
        self.key_proj = keras.layers.Dense(
            units=self.hidden_dim,
            dtype=dtype,
            name="key_proj",
        )
        self.value_proj = keras.layers.Dense(
            units=self.hidden_dim,
            dtype=dtype,
            name="value_proj",
        )
        self.out_proj = keras.layers.Dense(
            units=self.hidden_dim,
            dtype=dtype,
            name="out_proj",
        )

    def build(self, input_shape):
        self.query_proj.build([None, None, self.hidden_dim])
        self.key_proj.build([None, None, self.hidden_dim])
        self.value_proj.build([None, None, self.hidden_dim])
        self.out_proj.build([None, None, self.hidden_dim])
        self.built = True

    def _transpose_for_scores(self, tensor, batch_size):
        """
        Adapted from https://github.com/huggingface/transformers/blob/8e164c5400b7b413c7b8fb32e35132001effc970/src/transformers/models/bert/modeling_tf_bert.py#L252
        """
        # [batch_size, seq_len, all_head_dim] ->
        # [batch_size, seq_len, num_heads, head_dim]
        seq_len = ops.shape(tensor)[1]
        tensor = ops.reshape(
            tensor, (batch_size, seq_len, self.num_heads, self.head_dim)
        )
        # [batch_size, seq_len, num_heads, head_dim] ->
        # [batch_size, num_heads, seq_len, head_dim]
        return ops.transpose(tensor, axes=[0, 2, 1, 3])

    def call(
        self,
        x,
        attention_mask=None,
        return_attention_scores=None,
        training=False,
    ):
        batch_size = ops.shape(x)[0]
        mixed_query_layer = self.query_proj(inputs=x)
        mixed_key_layer = self.key_proj(inputs=x)
        mixed_value_layer = self.value_proj(inputs=x)
        query_layer = self._transpose_for_scores(mixed_query_layer, batch_size)
        key_layer = self._transpose_for_scores(mixed_key_layer, batch_size)
        value_layer = self._transpose_for_scores(mixed_value_layer, batch_size)

        # Scaled dot product between key and query = raw attention scores.
        attention_scores = ops.matmul(
            query_layer, ops.transpose(key_layer, axes=[0, 1, 3, 2])
        )
        dk = ops.cast(ops.sqrt(self.head_dim), dtype=attention_scores.dtype)
        attention_scores = ops.divide(
            attention_scores, dk
        )  # (batch_size, num_heads, seq_len_q, seq_len_k)

        if attention_mask is not None:
            # Apply the attention mask (precomputed for all layers in the
            # call() function)
            attention_scores = ops.add(attention_scores, attention_mask)

        # Normalize the attention scores to probabilities.
        attention_probs = ops.softmax(attention_scores, axis=-1)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        dropout_attention_probs = self.dropout_layer(
            inputs=attention_probs, training=training
        )

        attn_output = ops.matmul(dropout_attention_probs, value_layer)
        attn_output = ops.transpose(attn_output, axes=[0, 2, 1, 3])

        # (batch_size, seq_len_q, hidden_dim)
        seq_len_q = ops.shape(attn_output)[1]
        attn_output = ops.reshape(
            attn_output, (batch_size, seq_len_q, self.hidden_dim)
        )

        attn_output = self.out_proj(attn_output, training=training)
        return (attn_output, attention_probs)

    def get_config(self):
        config = super().get_config()
        config.update(
            {
                "hidden_dim": self.hidden_dim,
                "num_heads": self.num_heads,
                "dropout": self.dropout,
            }
        )
        return config


class Gemma3VisionEncoderLayer(keras.layers.Layer):
    def __init__(
        self,
        num_heads,
        intermediate_dim,
        layer_norm_epsilon=1e-6,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.num_heads = num_heads
        self.intermediate_dim = intermediate_dim
        self.layer_norm_epsilon = layer_norm_epsilon

    def compute_attention(self, x, mask=None):
        if mask is not None:
            mask = ops.cast(mask, dtype=x.dtype)
        return self.attn(x, attention_mask=mask)[0]

    def build(self, input_shape):
        hidden_dim = input_shape[-1]
        self.attn = Gemma3VisionAttention(
            hidden_dim,
            self.num_heads,
            dtype=self.dtype_policy,
            name="multi_head_attention",
        )
        self.layer_norm_1 = keras.layers.LayerNormalization(
            epsilon=self.layer_norm_epsilon,
            dtype=self.dtype_policy,
            name="layer_norm_1",
        )
        self.mlp_dense_1 = keras.layers.Dense(
            self.intermediate_dim,
            dtype=self.dtype_policy,
            name="mlp_dense_1",
        )
        self.mlp_dense_2 = keras.layers.Dense(
            hidden_dim,
            dtype=self.dtype_policy,
            name="mlp_dense_2",
        )
        self.layer_norm_2 = keras.layers.LayerNormalization(
            epsilon=self.layer_norm_epsilon,
            dtype=self.dtype_policy,
            name="layer_norm_2",
        )
        self.attn.build(None)
        self.layer_norm_1.build([None, None, hidden_dim])
        self.mlp_dense_1.build([None, None, hidden_dim])
        self.mlp_dense_2.build([None, None, self.intermediate_dim])
        self.layer_norm_2.build([None, None, hidden_dim])
        self.built = True

    def call(self, x, mask=None):
        residual = x
        x = self.layer_norm_1(x)
        # mask = ops.ones_like(x) if mask is None else mask
        x = self.compute_attention(x, mask)
        x = x + residual
        residual = x
        x = self.mlp_dense_1(self.layer_norm_2(residual))
        x = keras.activations.gelu(x, approximate=True)
        x = self.mlp_dense_2(x)
        return residual + x

    def compute_output_shape(self, inputs_shape):
        return inputs_shape

    def get_config(self):
        config = super().get_config()
        config.update(
            {
                "num_heads": self.num_heads,
                "intermediate_dim": self.intermediate_dim,
                "layer_norm_epsilon": self.layer_norm_epsilon,
            }
        )
        return config


class Gemma3VisionEncoderBlock(keras.layers.Layer):
    def __init__(
        self,
        patch_size,
        image_size,
        hidden_dim,
        num_layers,
        num_heads,
        intermediate_dim,
        layer_norm_epsilon=1e-6,
        dtype=None,
        **kwargs,
    ):
        super().__init__(dtype=dtype, **kwargs)
        self.patch_size = patch_size
        self.image_size = image_size
        self.hidden_dim = hidden_dim
        self.num_layers = num_layers
        self.num_heads = num_heads
        self.intermediate_dim = intermediate_dim
        self.layer_norm_epsilon = layer_norm_epsilon
        self.encoder_layer_norm = keras.layers.LayerNormalization(
            epsilon=layer_norm_epsilon,
            dtype=dtype,
            name="encoder_layer_norm",
        )
        self.vision_embeddings = Gemma3VisionEmbedding(
            hidden_dim=hidden_dim,
            patch_size=patch_size,
            image_size=image_size,
            dtype=dtype,
            name="encoder_embeddings",
        )
        self.resblocks = [
            Gemma3VisionEncoderLayer(
                self.num_heads,
                self.intermediate_dim,
                dtype=dtype,
                name=f"encoder_block_{i}",
            )
            for i in range(self.num_layers)
        ]

    def build(self, inputs_shape):
        # Collapse `batch_size`, dummy axis, `max_images_per_prompt` into one.
        inputs_shape = [None] + list(inputs_shape[2:])
        self.vision_embeddings.build(inputs_shape)
        for block in self.resblocks:
            block.build([None, None, self.hidden_dim])
        self.encoder_layer_norm.build([None, None, self.hidden_dim])
        self.built = True

    def call(self, inputs, mask=None):
        inputs_shape = ops.shape(inputs)

        # Collapse `batch_size`, dummy axis, `max_images_per_prompt` into one.
        inputs = ops.reshape(
            inputs,
            [inputs_shape[0] * inputs_shape[1]] + list(inputs_shape[2:]),
        )

        x = self.vision_embeddings(inputs)
        for block in self.resblocks:
            x = block(x, mask=mask)
        x = self.encoder_layer_norm(x)
        return x

    def compute_output_shape(self, inputs_shape):
        if inputs_shape is None:
            # Fix the compatibility issue with Keras 3.1 where
            # `compute_output_spec` fails to propagate `inputs_shape`
            # correctly, causing it to be `None`.
            return [None, None, self.hidden_dim]
        return [
            None,
            (inputs_shape[2] // self.patch_size) ** 2,
            self.hidden_dim,
        ]

    def get_config(self):
        config = super().get_config()
        config.update(
            {
                "hidden_dim": self.hidden_dim,
                "num_layers": self.num_layers,
                "num_heads": self.num_heads,
                "intermediate_dim": self.intermediate_dim,
                "patch_size": self.patch_size,
                "image_size": self.image_size,
                "layer_norm_epsilon": self.layer_norm_epsilon,
            }
        )
        return config


class Gemma3VisionAveragePooling(keras.layers.Layer):
    def __init__(self, image_size, patch_size, pool_size, **kwargs):
        super().__init__(**kwargs)

        self.width = image_size // patch_size
        # `reduced_width` is the same as `num_vision_tokens_per_image`.
        self.reduced_width = self.width // pool_size

        # Attributes.
        self.image_size = image_size
        self.patch_size = patch_size
        self.pool_size = pool_size

    def build(self, input_shape):
        self.average_pooling = keras.layers.AveragePooling2D(
            pool_size=self.pool_size,
            strides=self.pool_size,
            padding="valid",
            dtype=self.dtype_policy,
            name="average_pooling",
        )

    def call(self, x):
        # reshape `(bsz, height*width, emb_dim)` to
        # `(bsz, width, width, emb_dim)`. `height` should be equal to
        # `width`.
        batch_size, _, hidden_dim = ops.shape(x)
        x = ops.reshape(x, (batch_size, self.width, self.width, hidden_dim))
        x = self.average_pooling(x)
        output = ops.reshape(
            x, (batch_size, self.reduced_width * self.reduced_width, hidden_dim)
        )
        return output

    def compute_output_shape(self, input_shape):
        return (
            input_shape[0],
            self.reduced_width * self.reduced_width,
            input_shape[-1],
        )

    def get_config(self):
        config = super().get_config()
        config.update(
            {
                "image_size": self.image_size,
                "patch_size": self.patch_size,
                "pool_size": self.pool_size,
            }
        )
        return config


class Gemma3VisionOutput(keras.layers.Layer):
    def __init__(
        self,
        output_dim,
        layer_norm_epsilon=1e-6,
        kernel_initializer="glorot_uniform",
        **kwargs,
    ):
        super().__init__(**kwargs)

        self.layer_norm_epsilon = layer_norm_epsilon
        self.output_dim = output_dim

        self._kernel_initializer = keras.initializers.get(
            clone_initializer(kernel_initializer)
        )

    def build(self, input_shape):
        self.vision_soft_embedding_norm = RMSNormalization(
            epsilon=self.layer_norm_epsilon,
            dtype=self.dtype_policy,
            name="vision_soft_embedding_norm",
        )
        self.vision_soft_embedding_norm.build(input_shape)

        self.vision_input_projection = keras.layers.Dense(
            units=self.output_dim,
            use_bias=False,
            kernel_initializer=self._kernel_initializer,
            dtype=self.dtype_policy,
            name="vision_input_projection",
        )
        self.vision_input_projection.build(input_shape)

    def call(self, inputs):
        x = self.vision_soft_embedding_norm(inputs)
        x = self.vision_input_projection(x)
        return x

    def get_config(self):
        config = super().get_config()
        config.update(
            {
                "output_dim": self.output_dim,
                "layer_norm_epsilon": self.layer_norm_epsilon,
                "kernel_initializer": keras.initializers.serialize(
                    self._kernel_initializer
                ),
            }
        )

    def compute_output_shape(self, input_shape):
        return input_shape[:-1] + (self.output_dim,)
