"""
Layers using the cuda-convnet Theano wrappers that are part of pylearn2.
"""

import theano
import theano.tensor as T
import numpy as np

import layers

from theano.sandbox.cuda.basic_ops import gpu_contiguous
from pylearn2.sandbox.cuda_convnet.filter_acts import FilterActs
from pylearn2.sandbox.cuda_convnet.img_acts import ImageActs
from pylearn2.sandbox.cuda_convnet.pool import MaxPool, MaxPoolGrad
from pylearn2.sandbox.cuda_convnet.stochastic_pool import StochasticMaxPool
from pylearn2.sandbox.cuda_convnet.stochastic_pool import WeightedMaxPool
from pylearn2.sandbox.cuda_convnet.response_norm import CrossMapNorm
from theano.sandbox.cuda import host_from_gpu
from theano.tensor import as_tensor_variable

# TODO(tpaine) refactor the convolution layers to get rid of code repitition.


class CudaConvnetInput2DLayer(layers.Input2DLayer):
    """
    Like Input2DLayer, but the data is expected to be in c01b order instead of
    bc01.
    """
    def get_output_shape(self):
        # c01b instead of bc01
        return (self.n_features, self.width, self.height, self.mb_size)


class CudaConvnetConv2DLayer(object):
    def __init__(self,
                 input_layer,
                 n_filters,
                 filter_size,
                 weights_std,
                 init_bias_value,
                 stride=1,
                 nonlinearity=layers.rectify,
                 dropout=0.,
                 partial_sum=None,
                 pad=0,
                 untie_biases=False,
                 trainable=True):
        """
        Only the valid border mode is supported.

        n_filters should be a multiple of 16
        """
        self.input_layer = input_layer
        self.input_shape = self.input_layer.get_output_shape()
        self.n_filters = n_filters
        n_channels = self.input_shape[0]
        self.n_channels = n_channels
        self.filter_size = filter_size
        self.weights_std = np.float32(weights_std)
        self.init_bias_value = np.float32(init_bias_value)
        self.stride = stride
        self.nonlinearity = nonlinearity
        self.dropout = dropout
        self.partial_sum = partial_sum
        self.pad = pad
        self.untie_biases = untie_biases
        # if untie_biases == True, each position in the output map has its own
        # bias (as opposed to having the same bias everywhere for a given
        # filter)
        self.mb_size = self.input_layer.mb_size

        self.filter_shape = (n_channels, filter_size, filter_size, n_filters)

        self.trainable = trainable
        self.W = layers.shared_single(4)

        if self.untie_biases:
            self.b = layers.shared_single(3)
        else:
            self.b = layers.shared_single(1)

        self.params = [self.W, self.b]
        self.bias_params = [self.b]
        self.reset_params()

        self.filter_acts_op = FilterActs(stride=self.stride,
                                         partial_sum=self.partial_sum,
                                         pad=self.pad)

    def reset_params(self):
        self.W.set_value(np.random.randn(*self.filter_shape).astype(np.float32)
                         * self.weights_std)

        if self.untie_biases:
            self.b.set_value(
                np.ones(self.get_output_shape()[:3]).astype(np.float32)
                * self.init_bias_value)
        else:
            self.b.set_value(np.ones(self.n_filters).astype(np.float32)
                             * self.init_bias_value)

    def get_output_shape(self):
        output_width = int(np.ceil((self.input_shape[1] + 2 * self.pad - self.filter_size
                        + self.stride)*1.0 / self.stride))
        output_height = int(np.ceil((self.input_shape[2] + 2 * self.pad - self.filter_size
                         + self.stride)*1.0 / self.stride))
        output_shape = (self.n_filters, output_width, output_height,
                        self.mb_size)
        return output_shape

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            mask = layers.srng.binomial(input.shape, p=retain_prob,
                                        dtype='int32').astype('float32')
            # apply the input mask and rescale the input accordingly.
            # By doing this it's no longer necessary to rescale the weights
            # at test time.
            input = input / retain_prob * mask

        contiguous_input = gpu_contiguous(input)
        contiguous_filters = gpu_contiguous(self.W)
        conved = self.filter_acts_op(contiguous_input, contiguous_filters)

        if self.untie_biases:
            conved += self.b.dimshuffle(0, 1, 2, 'x')
        else:
            conved += self.b.dimshuffle(0, 'x', 'x', 'x')

        return self.nonlinearity(conved)


class CudaConvnetConv2DNoBiasLayer(object):
    def __init__(self,
                 input_layer,
                 n_filters,
                 filter_size,
                 weights_std,
                 stride=1,
                 nonlinearity=layers.rectify,
                 dropout=0.,
                 partial_sum=None,
                 pad=0,
                 trainable=True):
        """
        Only the valid border mode is supported.

        n_filters should be a multiple of 16
        """
        self.input_layer = input_layer
        self.input_shape = self.input_layer.get_output_shape()
        self.n_filters = n_filters
        n_channels = self.input_shape[0]
        self.n_channels = n_channels
        self.filter_size = filter_size
        self.weights_std = np.float32(weights_std)
        self.stride = stride
        self.nonlinearity = nonlinearity
        self.dropout = dropout
        self.partial_sum = partial_sum
        self.pad = pad
        self.mb_size = self.input_layer.mb_size

        self.filter_shape = (n_channels, filter_size, filter_size, n_filters)

        self.trainable = trainable
        self.W = layers.shared_single(4)

        self.params = [self.W]
        self.reset_params()

        self.filter_acts_op = FilterActs(stride=self.stride,
                                         partial_sum=self.partial_sum,
                                         pad=self.pad)

    def reset_params(self):
        self.W.set_value(np.random.randn(*self.filter_shape).astype(np.float32)
                         * self.weights_std)

    def get_output_shape(self):
        output_width = int(np.ceil((self.input_shape[1] + 2 * self.pad - self.filter_size
                        + self.stride)*1.0 / self.stride))
        output_height = int(np.ceil((self.input_shape[2] + 2 * self.pad - self.filter_size
                         + self.stride)*1.0 / self.stride))
        output_shape = (self.n_filters, output_width, output_height,
                        self.mb_size)
        return output_shape

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            mask = layers.srng.binomial(input.shape, p=retain_prob,
                                        dtype='int32').astype('float32')
            # apply the input mask and rescale the input accordingly.
            # By doing this it's no longer necessary to rescale the weights
            # at test time.
            input = input / retain_prob * mask

        contiguous_input = gpu_contiguous(input)
        contiguous_filters = gpu_contiguous(self.W)
        conved = self.filter_acts_op(contiguous_input, contiguous_filters)

        return self.nonlinearity(conved)


class ACudaConvnetConv2DLayer(CudaConvnetConv2DLayer):
    def __init__(self,
                 input_layer,
                 n_filters,
                 filter_size,
                 weights_std,
                 init_bias_value,
                 stride=1,
                 nonlinearity=layers.a_rectify,
                 dropout=0.,
                 partial_sum=None,
                 pad=0,
                 untie_biases=False,
                 trainable=True):
        self.alpha = theano.shared(np.array(0.0, dtype=theano.config.floatX))
        super(ACudaConvnetConv2DLayer, self).__init__(
            input_layer,
            n_filters,
            filter_size,
            weights_std,
            init_bias_value,
            stride=stride,
            nonlinearity=nonlinearity,
            dropout=dropout,
            partial_sum=partial_sum,
            pad=pad,
            untie_biases=untie_biases,
            trainable=trainable)

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            mask = layers.srng.binomial(input.shape, p=retain_prob,
                                        dtype='int32').astype('float32')
            # apply the input mask and rescale the input accordingly.
            # By doing this it's no longer necessary to rescale the weights
            # at test time.
            input = input / retain_prob * mask

        contiguous_input = gpu_contiguous(input)
        contiguous_filters = gpu_contiguous(self.W)
        conved = self.filter_acts_op(contiguous_input, contiguous_filters)

        if self.untie_biases:
            conved += self.b.dimshuffle(0, 1, 2, 'x')
        else:
            conved += self.b.dimshuffle(0, 'x', 'x', 'x')

        return self.nonlinearity(conved, self.alpha)


class CudaConvnetDeconv2DLayer(object):
    def __init__(self,
                 input_layer,
                 mirror_layer,
                 nonlinearity=None):
        """
        Only the valid border mode is supported.

        n_filters should be a multiple of 16
        """

        self.mirror_layer = mirror_layer

        self.input_layer = input_layer
        self.input_shape = self.input_layer.get_output_shape()
        n_filters = self.input_shape[0]

        if nonlinearity:
            self.nonlinearity = nonlinearity
        else:
            self.nonlinearity = mirror_layer.nonlinearity

        self.n_channels = mirror_layer.n_channels
        self.n_filters = mirror_layer.n_filters
        self.filter_size = mirror_layer.filter_size
        self.weights_std = mirror_layer.weights_std
        self.init_bias_value = mirror_layer.init_bias_value
        self.stride = mirror_layer.stride
        self.dropout = mirror_layer.dropout
        self.partial_sum = mirror_layer.partial_sum
        self.pad = mirror_layer.pad
        self.untie_biases = mirror_layer.untie_biases
        # if untie_biases == True, each position in the output map has its own
        # bias (as opposed to having the same bias everywhere for a filter)
        self.mb_size = self.input_layer.mb_size

        self.filter_shape = mirror_layer.filter_shape

        self.trainable = False
        self.W = mirror_layer.W

        self.b = mirror_layer.b

        # self.params = [self.W, self.b]
        self.params = []
        self.bias_params = [self.b]

        self.image_acts_op = ImageActs(stride=self.stride,
                                       partial_sum=self.partial_sum,
                                       pad=self.pad)

    def get_output_shape(self):
        output_shape = self.mirror_layer.input_layer.get_output_shape()
        return output_shape

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)

        if self.untie_biases:
            input -= self.b.dimshuffle(0, 1, 2, 'x')
        else:
            input -= self.b.dimshuffle(0, 'x', 'x', 'x')

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            mask = layers.srng.binomial(input.shape, p=retain_prob,
                                        dtype='int32').astype('float32')
            # apply the input mask and rescale the input accordingly.
            # By doing this it's no longer necessary to rescale the weights
            # at test time.
            input = input / retain_prob * mask

        contiguous_input = gpu_contiguous(input)
        contiguous_filters = gpu_contiguous(self.W)
        if self.stride == 1:
            deconved = self.image_acts_op(contiguous_input, contiguous_filters)
        else:
            _, x, y, _ = self.get_output_shape()
            deconved = self.image_acts_op(contiguous_input, contiguous_filters,
                                          as_tensor_variable((x, y)))
        return self.nonlinearity(deconved)


class CudaConvnetDeconvUntied2DLayer(object):
    def __init__(self,
                 input_layer,
                 mirror_layer,
                 nonlinearity=None):
        """
        Only the valid border mode is supported.

        n_filters should be a multiple of 16
        """

        self.mirror_layer = mirror_layer

        self.input_layer = input_layer
        self.input_shape = self.input_layer.get_output_shape()
        n_filters = self.input_shape[0]

        if nonlinearity:
            self.nonlinearity = nonlinearity
        else:
            self.nonlinearity = mirror_layer.nonlinearity

        self.n_channels = mirror_layer.n_channels
        self.n_filters = mirror_layer.n_filters
        self.filter_size = mirror_layer.filter_size
        self.weights_std = mirror_layer.weights_std
        self.init_bias_value = mirror_layer.init_bias_value
        self.stride = mirror_layer.stride
        self.dropout = mirror_layer.dropout
        self.partial_sum = mirror_layer.partial_sum
        self.pad = mirror_layer.pad
        self.untie_biases = mirror_layer.untie_biases

        self.mb_size = self.input_layer.mb_size

        self.filter_shape = mirror_layer.filter_shape

        self.trainable = False
        self.W = layers.shared_single(4)

        if self.untie_biases:
            self.b = layers.shared_single(3)
        else:
            self.b = layers.shared_single(1)

        # self.params = [self.W, self.b]
        self.params = [self.W, self.b]
        self.bias_params = [self.b]
        self.reset_params()

        self.image_acts_op = ImageActs(stride=self.stride,
                                       partial_sum=self.partial_sum,
                                       pad=self.pad)

    def reset_params(self):
        self.W.set_value(np.random.randn(*self.filter_shape).astype(np.float32)
                         * self.weights_std)

        if self.untie_biases:
            self.b.set_value(
                np.ones(self.get_output_shape()[:3]).astype(np.float32)
                * self.init_bias_value)
        else:
            self.b.set_value(np.ones(self.n_filters).astype(np.float32)
                             * self.init_bias_value)

    def get_output_shape(self):
        output_shape = self.mirror_layer.input_layer.get_output_shape()
        return output_shape

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)

        if self.untie_biases:
            input -= self.b.dimshuffle(0, 1, 2, 'x')
        else:
            input -= self.b.dimshuffle(0, 'x', 'x', 'x')

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            mask = layers.srng.binomial(input.shape, p=retain_prob,
                                        dtype='int32').astype('float32')
            # apply the input mask and rescale the input accordingly.
            # By doing this it's no longer necessary to rescale the weights
            # at test time.
            input = input / retain_prob * mask

        contiguous_input = gpu_contiguous(input)
        contiguous_filters = gpu_contiguous(self.W)
        if self.stride == 1:
            deconved = self.image_acts_op(contiguous_input, contiguous_filters)
        else:
            _, x, y, _ = self.get_output_shape()
            deconved = self.image_acts_op(contiguous_input, contiguous_filters,
                                          as_tensor_variable((x, y)))
        return self.nonlinearity(deconved)


class CudaConvnetDeconv2DNoBiasLayer(object):
    def __init__(self,
                 input_layer,
                 mirror_layer,
                 nonlinearity=None):
        """
        Only the valid border mode is supported.

        n_filters should be a multiple of 16
        """

        self.mirror_layer = mirror_layer

        self.input_layer = input_layer
        self.input_shape = self.input_layer.get_output_shape()
        n_filters = self.input_shape[0]

        if nonlinearity:
            self.nonlinearity = nonlinearity
        else:
            self.nonlinearity = mirror_layer.nonlinearity

        self.n_channels = mirror_layer.n_channels
        self.n_filters = mirror_layer.n_filters
        self.filter_size = mirror_layer.filter_size
        self.weights_std = mirror_layer.weights_std
        self.stride = mirror_layer.stride
        self.dropout = mirror_layer.dropout
        self.partial_sum = mirror_layer.partial_sum
        self.pad = mirror_layer.pad
        self.mb_size = self.input_layer.mb_size

        self.filter_shape = mirror_layer.filter_shape

        self.trainable = False
        self.W = mirror_layer.W

        self.params = []

        self.image_acts_op = ImageActs(stride=self.stride,
                                       partial_sum=self.partial_sum,
                                       pad=self.pad)

    def get_output_shape(self):
        output_shape = self.mirror_layer.input_layer.get_output_shape()
        return output_shape

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            mask = layers.srng.binomial(input.shape, p=retain_prob,
                                        dtype='int32').astype('float32')
            # apply the input mask and rescale the input accordingly.
            # By doing this it's no longer necessary to rescale the weights
            # at test time.
            input = input / retain_prob * mask

        contiguous_input = gpu_contiguous(input)
        contiguous_filters = gpu_contiguous(self.W)
        if self.stride == 1:
            deconved = self.image_acts_op(contiguous_input, contiguous_filters)
        else:
            _, x, y, _ = self.get_output_shape()
            deconved = self.image_acts_op(contiguous_input, contiguous_filters,
                                          as_tensor_variable((x, y)))
        return self.nonlinearity(deconved)


class CudaConvnetDeconv2DNoBiasNormedLayer(object):
    def __init__(self,
                 input_layer,
                 mirror_layer,
                 nonlinearity=None):
        """
        Only the valid border mode is supported.

        n_filters should be a multiple of 16
        """

        self.mirror_layer = mirror_layer

        self.input_layer = input_layer
        self.input_shape = self.input_layer.get_output_shape()
        n_filters = self.input_shape[0]

        if nonlinearity:
            self.nonlinearity = nonlinearity
        else:
            self.nonlinearity = mirror_layer.nonlinearity

        self.n_channels = mirror_layer.n_channels
        self.n_filters = mirror_layer.n_filters
        self.filter_size = mirror_layer.filter_size
        self.weights_std = mirror_layer.weights_std
        self.stride = mirror_layer.stride
        self.dropout = mirror_layer.dropout
        self.partial_sum = mirror_layer.partial_sum
        self.pad = mirror_layer.pad
        self.mb_size = self.input_layer.mb_size

        self.filter_shape = mirror_layer.filter_shape

        self.trainable = False
        self.W = mirror_layer.W

        self.params = []

        self.image_acts_op = ImageActs(stride=self.stride,
                                       partial_sum=self.partial_sum,
                                       pad=self.pad)

    def get_output_shape(self):
        output_shape = self.mirror_layer.input_layer.get_output_shape()
        return output_shape

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            mask = layers.srng.binomial(input.shape, p=retain_prob,
                                        dtype='int32').astype('float32')
            # apply the input mask and rescale the input accordingly.
            # By doing this it's no longer necessary to rescale the weights
            # at test time.
            input = input / retain_prob * mask

        contiguous_input = gpu_contiguous(input)
        contiguous_filters = gpu_contiguous(self.W)
        if self.stride == 1:
            deconved = self.image_acts_op(contiguous_input, contiguous_filters)
        else:
            _, x, y, _ = self.get_output_shape()
            deconved = self.image_acts_op(contiguous_input, contiguous_filters,
                                          as_tensor_variable((x, y)))

        out = self.nonlinearity(deconved)
        norm_input = T.sqrt(T.sum(self.mirror_layer.input_layer.output()**2,
                            axis=(0, 1, 2)))[None, None, None, :]
        norm_out = T.sqrt(T.sum(out**2, axis=(0, 1, 2)))[None, None, None, :]
        return out/norm_out*norm_input


class ACudaConvnetDeconv2DLayer(CudaConvnetDeconv2DLayer):
    def __init__(self,
                 input_layer,
                 mirror_layer):
        self.alpha = theano.shared(np.array(0.0, dtype=theano.config.floatX))
        super(ACudaConvnetDeconv2DLayer, self).__init__(input_layer,
                                                        mirror_layer)

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)

        if self.untie_biases:
            input -= self.b.dimshuffle(0, 1, 2, 'x')
        else:
            input -= self.b.dimshuffle(0, 'x', 'x', 'x')

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            mask = layers.srng.binomial(input.shape, p=retain_prob,
                                        dtype='int32').astype('float32')
            # apply the input mask and rescale the input accordingly.
            # By doing this it's no longer necessary to rescale the weights
            # at test time.
            input = input / retain_prob * mask

        contiguous_input = gpu_contiguous(input)
        contiguous_filters = gpu_contiguous(self.W)
        if self.stride == 1:
            deconved = self.image_acts_op(contiguous_input, contiguous_filters)
        else:
            deconved = self.image_acts_op(contiguous_input, contiguous_filters,
                                          as_tensor_variable(self.shape))

        return self.nonlinearity(deconved, self.alpha)


class CudaConvnetPooling2DLayer(object):
    def __init__(self, input_layer, pool_size, stride=None):
        """
        pool_size is an INTEGER, not a tuple. We can only do square pooling.
        If the stride is none, it is taken to be the same as the pool size.

        borders are never ignored.
        """
        self.pool_size = pool_size
        self.stride = stride if stride is not None else pool_size
        self.input_layer = input_layer
        self.trainable = False
        self.params = []
        self.bias_params = []
        self.mb_size = self.input_layer.mb_size

        self.pool_op = MaxPool(ds=self.pool_size, stride=self.stride)

    def get_output_shape(self):
        input_shape = self.input_layer.get_output_shape()
        w, h = input_shape[1], input_shape[2]

        new_w = int(np.ceil(float(w - self.pool_size + self.stride)
                            / self.stride))
        new_h = int(np.ceil(float(h - self.pool_size + self.stride)
                            / self.stride))

        return (input_shape[0], new_w, new_h, input_shape[3])

    def output(self, *args, **kwargs):
        input = self.input_layer.output(*args, **kwargs)
        contiguous_input = gpu_contiguous(input)
        return self.pool_op(contiguous_input)


class CudaConvnetUnpooling2DLayer(object):
    def __init__(self, input_layer, pooling_layer):
        """
        pool_size is an INTEGER, not a tuple. We can only do square pooling.
        if the stride is none, it is taken to be the same as the pool size.

        borders are never ignored.
        """
        self.pool_size = pooling_layer.pool_size
        self.stride = pooling_layer.stride
        self.input_layer = input_layer
        self.pooling_layer = pooling_layer
        self.trainable = False
        self.params = []
        self.bias_params = []
        self.mb_size = self.input_layer.mb_size

        self.unpool_op = MaxPoolGrad(ds=self.pool_size, stride=self.stride,
                                     start=0)

    def get_output_shape(self):
        shape = self.pooling_layer.input_layer.get_output_shape()
        return shape

    def output(self, *args, **kwargs):
        input = self.input_layer.output()
        max_out = self.pooling_layer.output()
        orig_input = self.pooling_layer.input_layer.output()
        return self.unpool_op(orig_input, max_out, input)


class CudaConvnetStochasticPooling2DLayer(object):
    def __init__(self, input_layer, pool_size, stride=None):
        """
        This implements stochastic pooling as in Zeiler et al. 2013 to
        replace max pooling. Pooling is stochastic by default. When
        dropout_active=True, weighted pooling is used instead. As a result it
        is not possible to enable/disable stochastic pooling and dropout
        separately within a network, but the use cases for that should be rare.
        Usually we want both on during training, and both off at test time.

        pool_size is an INTEGER, not a tuple. We can only do square pooling.
        if the stride is none, it is taken to be the same as the pool size.

        borders are never ignored.
        """
        self.pool_size = pool_size
        self.stride = stride if stride is not None else pool_size
        self.input_layer = input_layer
        self.trainable = False
        self.params = []
        self.bias_params = []
        self.mb_size = self.input_layer.mb_size

        self.stochastic_pool_op = StochasticMaxPool(ds=self.pool_size,
                                                    stride=self.stride)
        self.weighted_pool_op = WeightedMaxPool(ds=self.pool_size,
                                                stride=self.stride)

    def get_output_shape(self):
        input_shape = self.input_layer.get_output_shape()
        w, h = input_shape[1], input_shape[2]

        new_w = int(np.ceil(float(w - self.pool_size + self.stride)
                            / self.stride))
        new_h = int(np.ceil(float(h - self.pool_size + self.stride)
                            / self.stride))

        return (input_shape[0], new_w, new_h, input_shape[3])

    def output(self, dropout_active=True, *args, **kwargs):
        input = self.input_layer.output(dropout_active=dropout_active,
                                        *args, **kwargs)
        contiguous_input = gpu_contiguous(input)

        if dropout_active:
            return self.stochastic_pool_op(contiguous_input)
        else:
            return self.weighted_pool_op(contiguous_input)


class CudaConvnetCrossMapNormLayer(object):
    def __init__(self,
                 input_layer,
                 alpha=1e-4,
                 beta=0.75,
                 size_f=5,
                 blocked=True):
        self.alpha = alpha
        self.beta = beta
        self.size_f = size_f
        self.blocked = blocked
        self.input_layer = input_layer
        self.trainable = False
        self.params = []
        self.bias_params = []
        self.mb_size = self.input_layer.mb_size

        self.norm_op = CrossMapNorm(size_f=size_f, add_scale=alpha,
                                    pow_scale=beta, blocked=blocked)

    def get_output_shape(self):
        # output shape is the same as the input shape
        return self.input_layer.get_output_shape()

    def output(self, *args, **kwargs):
        input = self.input_layer.output(*args, **kwargs)
        contiguous_input = gpu_contiguous(input)
        return self.norm_op(contiguous_input)[0]


class ShuffleC01BToBC01Layer(object):
    """
    This layer dimshuffles 4D input for interoperability for C01B and BC01 ops.
    C01B (cuda convnet) -> BC01 (theano)
    """
    def __init__(self, input_layer):
        self.input_layer = input_layer
        self.trainable = False
        self.params = []
        self.bias_params = []
        self.mb_size = self.input_layer.mb_size

    def get_output_shape(self):
        input_shape = self.input_layer.get_output_shape()
        return (input_shape[3], input_shape[0], input_shape[1], input_shape[2])

    def output(self, *args, **kwargs):
        input = self.input_layer.output(*args, **kwargs)
        return input.dimshuffle(3, 0, 1, 2)


class ShuffleBC01ToC01BLayer(object):
    """
    This layer dimshuffles 4D input for interoperability for C01B and BC01 ops.
    BC01 (theano) -> C01B (cuda convnet)
    """
    def __init__(self, input_layer):
        self.input_layer = input_layer
        self.trainable = False
        self.params = []
        self.bias_params = []
        self.mb_size = self.input_layer.mb_size

    def get_output_shape(self):
        input_shape = self.input_layer.get_output_shape()
        return (input_shape[1], input_shape[2], input_shape[3], input_shape[0])

    def output(self, *args, **kwargs):
        input = self.input_layer.output(*args, **kwargs)
        return input.dimshuffle(1, 2, 3, 0)


class CudaConvnetCircularConv2DLayer(object):
    def __init__(self,
                 input_layer,
                 n_filters,
                 filter_size,
                 weights_std,
                 init_bias_value,
                 stride=1,
                 nonlinearity=layers.rectify,
                 dropout=0.,
                 partial_sum=None,
                 untie_biases=False,
                 trainable=True):
        """
        This is a convolution which is circular in the 0-direction, and valid
        in the 1-direction.

        n_filters should be a multiple of 16
        """
        self.input_layer = input_layer
        self.n_filters = n_filters
        self.filter_size = filter_size
        self.weights_std = np.float32(weights_std)
        self.init_bias_value = np.float32(init_bias_value)
        self.stride = stride
        self.nonlinearity = nonlinearity
        self.dropout = dropout
        self.partial_sum = partial_sum
        self.untie_biases = untie_biases
        self.mb_size = self.input_layer.mb_size

        self.input_shape = self.input_layer.get_output_shape()

        self.filter_shape = (self.input_shape[0], filter_size, filter_size,
                             n_filters)

        self.trainable = trainable
        self.W = layers.shared_single(4)

        if self.untie_biases:
            self.b = layers.shared_single(3)
        else:
            self.b = layers.shared_single(1)

        self.params = [self.W, self.b]
        self.bias_params = [self.b]
        self.reset_params()

        self.filter_acts_op = FilterActs(stride=self.stride,
                                         partial_sum=self.partial_sum)

    def reset_params(self):
        self.W.set_value(np.random.randn(*self.filter_shape).astype(np.float32)
                         * self.weights_std)

        if self.untie_biases:
            self.b.set_value(
                np.ones(self.get_output_shape()[:3]).astype(np.float32)
                * self.init_bias_value)
        else:
            self.b.set_value(np.ones(self.n_filters).astype(np.float32)
                             * self.init_bias_value)

    def get_output_shape(self):
        # because it's a circular convolution, this dimension is just divided
        # by the stride.
        output_width = self.input_shape[1] // self.stride
        # in this direction it's still valid though.
        output_height = ((self.input_shape[2] - self.filter_size + self.stride)
                         // self.stride)
        output_shape = (self.n_filters, output_width, output_height,
                        self.mb_size)
        return output_shape

    def output(self, input=None, dropout_active=True, *args, **kwargs):
        if input is None:
            input = self.input_layer.output(dropout_active=dropout_active,
                                            *args, **kwargs)

        if dropout_active and (self.dropout > 0.):
            retain_prob = 1 - self.dropout
            mask = layers.srng.binomial(input.shape, p=retain_prob,
                                        dtype='int32').astype('float32')
            # apply the input mask and rescale the input accordingly.
            # By doing this it's no longer necessary to rescale the weights
            # at test time.
            input = input / retain_prob * mask

        # pad input so the valid convolution amounts to a circular one.
        # we need to copy (filter_size - stride) values from one side to
        # the other
        input_padded = T.zeros((input.shape[0], input.shape[1]
                               + self.filter_size - self.stride,
                               input.shape[2], input.shape[3]))
        input_padded = T.set_subtensor(input_padded[:, :input.shape[1], :, :],
                                       input)
        input_padded = T.set_subtensor(input_padded[:, input.shape[1]:, :, :],
                                       input[:,
                                             :self.filter_size - self.stride,
                                             :,
                                             :])

        contiguous_input = gpu_contiguous(input_padded)
        contiguous_filters = gpu_contiguous(self.W)
        conved = self.filter_acts_op(contiguous_input, contiguous_filters)

        if self.untie_biases:
            conved += self.b.dimshuffle(0, 1, 2, 'x')
        else:
            conved += self.b.dimshuffle(0, 'x', 'x', 'x')

        return self.nonlinearity(conved)


# TODO(tpaine) remove this layer
def shuffle_pool_unshuffle(input_layer, *args, **kwargs):
    """
    The Krizhevskhy max pooling layer only supports square input. This function
    provides a workaround that uses Theano's own max pooling op, flanked by two
    shuffling operations: c01b to bc01 before pooling, and bc01 to c01b
    afterwards.
    """
    l_bc01 = ShuffleC01BToBC01Layer(input_layer)
    l_pool = layers.Pooling2DLayer(l_bc01, *args, **kwargs)
    l_c01b = ShuffleBC01ToC01BLayer(l_pool)

    return l_c01b


# TODO(tpaine) remove this layer
class StochasticPoolingC01BLayer(object):
    """
    Stochastic pooling implemented in Theano using reshapes, since the Pylearn2
    class for it is way too slow.

    This only works for c01b, i.e. it assumes that the dimensions to pool over
    are (1, 2). It's also required that the dimensions are a multiple of the
    pool size (no incomplete pools).

    epsilon is used to prevent division by 0, it is added to all probabilities,
    so that when all activations are 0, the distribution is uniform.
    """
    def __init__(self, input_layer, pool_size, epsilon=1e-12):
        """
        pool_size: the number of inputs to be pooled together.
        """
        self.pool_size = pool_size
        self.epsilon = epsilon
        self.input_layer = input_layer
        self.input_shape = self.input_layer.get_output_shape()
        self.mb_size = self.input_layer.mb_size

        self.trainable = False
        self.params = []
        self.bias_params = []

    def get_output_shape(self):
        output_shape = list(self.input_shape)  # make a mutable copy
        output_shape[1] = output_shape[1] // self.pool_size
        output_shape[2] = output_shape[2] // self.pool_size
        return tuple(output_shape)

    def output(self, dropout_active=True, *args, **kwargs):
        input = self.input_layer.output(*args, **kwargs)

        output_shape = self.get_output_shape()
        pool_shape = (output_shape[0], output_shape[1], self.pool_size,
                      output_shape[2], self.pool_size, output_shape[3])
        merged_shape = (output_shape[0], output_shape[1], output_shape[2],
                        output_shape[3], self.pool_size**2)
        flat_shape = (output_shape[0] * output_shape[1] * output_shape[2]
                      * output_shape[3], self.pool_size**2)
        input_reshaped = input.reshape(
            pool_shape).transpose(0, 1, 3, 5, 2, 4).reshape(flat_shape)
        # pools are now in axis 4

        # add a small constant to prevent division by 0 in what follows.
        input_reshaped += self.epsilon

        if dropout_active:
            probabilities = input_reshaped / input_reshaped.sum(axis=1,
                                                                keepdims=True)
            samples = layers.srng.multinomial(pvals=probabilities,
                                              dtype=theano.config.floatX)
            output_flat = T.sum(input_reshaped * samples, axis=1)
            output = output_flat.reshape(output_shape)
        else:
            # no dropout, so compute the weighted average instead.
            # this amounts to the sum of squares normalised by the sum of the
            # values.
            numerator = T.sum(input_reshaped**2, axis=1)
            denominator = T.sum(input_reshaped, axis=1)
            output_flat = numerator / denominator
            output = output_flat.reshape(output_shape)
        return output


# TODO(tpaine) remove this layer
class LcnLayer(object):
    def __init__(self, input_layer, filter_size=3, num_channels=96,
                 num_filters=96):
        self.input_layer = input_layer
        self.filter_size = filter_size
        self.num_channels = num_channels
        self.num_filters = num_filters

        self.trainable = False
        self.params = []
        self.bias_params = []
        self.mb_size = self.input_layer.mb_size

        self.conv_func = FilterActs(pad=self.filter_size/2)
        n = self.num_channels * self.filter_size * self.filter_size
        self.w = np.float32(np.ones((self.num_channels, self.filter_size,
                                     self.filter_size, self.num_filters)))/n

    def get_output_shape(self):
        # output shape is the same as the input shape
        return self.input_layer.get_output_shape()

    def output(self, *args, **kwargs):

        input = self.input_layer.output(*args, **kwargs)
        gpu_input = gpu_contiguous(input)
        gpu_filter = gpu_contiguous(self.w)

        mean_batch_symbol = self.conv_func(gpu_input, gpu_filter)

        diff_batch_symbol = (input - mean_batch_symbol)
        gpu_diff_sq = gpu_contiguous(diff_batch_symbol**2)

        std_batch_symbol = self.conv_func(gpu_diff_sq, gpu_filter)
        norm_batch_symbol = diff_batch_symbol / (std_batch_symbol**(1/2))

        return norm_batch_symbol
