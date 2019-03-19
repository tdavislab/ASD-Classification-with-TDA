import torch
from torch import nn
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.svm import SVC
import sklearn_tda
import warnings
from slayer import SLayer, UpperDiagonalThresholdedLogTransform
import numpy as np
import pandas as pd
from time import time
from tqdm.autonotebook import tqdm
from sklearn.metrics.cluster import contingency_matrix
import statsmodels


def create_linear(layer_shapes, dropout_prob):
    linear = nn.Sequential()

    for layer_num, (num_in, num_out) in enumerate(zip(layer_shapes, layer_shapes[1:])):
        linear.add_module('linear_{}'.format(layer_num), nn.Linear(num_in, num_out))
        linear.add_module('batchnorm_{}'.format(layer_num), nn.BatchNorm1d(num_out))
        if layer_num != len(layer_shapes) - 2:
            linear.add_module('dropout_{}'.format(layer_num), nn.Dropout(dropout_prob))
            linear.add_module('relu_{}'.format(layer_num), nn.ReLU())

    return linear


def create_slayer_linear(layer_shapes, dropout_prob):
    linear = nn.Sequential()

    for layer_num, (num_in, num_out) in enumerate(zip(layer_shapes, layer_shapes[1:])):
        if layer_num == 0:
            linear.add_module('slayer_{}'.format(layer_num), SLayer(num_in, 2))

        linear.add_module('linear_{}'.format(layer_num), nn.Linear(num_in, num_out))
        linear.add_module('batchnorm_{}'.format(layer_num), nn.BatchNorm1d(num_out))

        if layer_num != len(layer_shapes) - 2:
            linear.add_module('dropout_{}'.format(layer_num), nn.Dropout(dropout_prob))
            linear.add_module('relu_{}'.format(layer_num), nn.ReLU())

    return linear


class NNVec(nn.Module):
    def __init__(self, layer_shapes, dropout_prob=0.1):
        super(NNVec, self).__init__()
        self.linear = create_linear(layer_shapes, dropout_prob)

    def forward(self, batch):
        # x = torch.stack(batch)
        x = nn.functional.relu(self.linear(batch))

        return nn.functional.softmax(x, dim=1)


class NNPersDiag(nn.Module):

    def __init__(self, pers_layers_shapes, merge_layer_shape, dropout_prob=0.1):
        super(NNPersDiag, self).__init__()

        self.branches = [create_slayer_linear(branch_shape, dropout_prob) for branch_shape in pers_layers_shapes]

        for i, branch in enumerate(self.branches):
            self.add_module('branch_{}'.format(i), branch)

        if sum(map(lambda x: x[-1], pers_layers_shapes)) != merge_layer_shape[0]:
            warnings.warn('Layer shape mismatch between first layer of merge and last layers of parallel modules',
                          UserWarning)

        self.merge_layer = create_linear(merge_layer_shape, dropout_prob)

    def forward(self, pers_dim0, pers_dim1):
        inputs = [pers_dim0, pers_dim1]
        x = torch.cat([branch(branch_input) for branch, branch_input in zip(self.branches, inputs)], 1)
        x = self.merge_layer(x)

        return nn.functional.softmax(x, dim=1)


class NNHybridVec(nn.Module):
    def __init__(self, branchwise_shapes, merge_layer_shape, dropout_prob=0.1):
        super(NNHybridVec, self).__init__()

        self.branches = [create_linear(branch_shape, dropout_prob) for branch_shape in branchwise_shapes]

        for i, branch in enumerate(self.branches):
            self.add_module('branch_{}'.format(i), branch)

        if sum(map(lambda x: x[-1], branchwise_shapes)) != merge_layer_shape[0]:
            warnings.warn('Layer shape mismatch between first layer of merge and last layers of parallel modules',
                          UserWarning)

        self.merge_layer = create_linear(merge_layer_shape, dropout_prob)
        self.add_module('merge_layer', self.merge_layer)

    def forward(self, vec_feature_1, vec_feature_2):
        inputs = [vec_feature_1, vec_feature_2]
        x = torch.cat([branch(branch_input) for branch, branch_input in zip(self.branches, inputs)], 1)
        x = self.merge_layer(x)

        return nn.functional.softmax(x, dim=1)


class NNHybridPers(nn.Module):
    def __init__(self, pers_layers_shapes, vec_layer_shapes, merge_layer_shape, dropout_prob=0.1):
        super(NNHybridPers, self).__init__()

        self.branches = [create_slayer_linear(branch_shape, dropout_prob) for branch_shape in pers_layers_shapes]
        if isinstance(vec_layer_shapes[0], list):
            self.branches += [create_linear(branch_shape, dropout_prob) for branch_shape in vec_layer_shapes]
        else:
            self.branches.append(create_linear(vec_layer_shapes, dropout_prob))

        for i, branch in enumerate(self.branches):
            self.add_module('branch_{}'.format(i), branch)

        if sum(map(lambda x: x[-1], pers_layers_shapes + [vec_layer_shapes])) != merge_layer_shape[0]:
            warnings.warn('Layer shape mismatch between first layer of merge and last layers of parallel modules',
                          UserWarning)

        self.merge_layer = create_linear(merge_layer_shape, dropout_prob)
        self.add_module('merge_layer', self.merge_layer)

    def forward(self, pers_dim0, pers_dim1, corr_features):
        inputs = [pers_dim0, pers_dim1, corr_features]
        x = torch.cat([branch(branch_input) for branch, branch_input in zip(self.branches, inputs)], 1)
        x = self.merge_layer(x)

        return nn.functional.softmax(x, dim=1)


def get_kernel(kernel='scale_space', weights=(0.5, 0.5)):
    """
    Return a kernel function for use in kernel methods
    :param kernel: type of persistence kernel to use, should be one of 'scale space',
        'weighted gaussian', 'sliced_wasserstein' or 'fisher'. The same kernel is used
        across all homology dimensions
    :param weights: scalar factor to weigh each dimension's gram matrix
    :return: sum of weighted gram matrices
    """

    # kernel_approx = RBFSampler(gamma=0.5, n_components=100000).fit(np.ones([1,2]))
    kernel_approx = None

    kernels = {
        'scale_space': sklearn_tda.PersistenceScaleSpaceKernel(kernel_approx=kernel_approx),
        'weighted_gaussian': sklearn_tda.PersistenceWeightedGaussianKernel(kernel_approx=kernel_approx),
        'sliced_wasserstein': sklearn_tda.SlicedWassersteinKernel(),
        'fisher': sklearn_tda.PersistenceFisherKernel(kernel_approx=kernel_approx),
    }

    if kernel not in kernels.keys():
        raise KeyError("Specified kernel not found. Make sure it is one "
                       "of ['scale_space', 'weighted_gaussian', 'sliced_wasserstein', 'fisher']")

    k = kernels[kernel]

    def kernel_wrapper(data1, data2):

        kernel_matrices = []

        for dim, w in enumerate(weights):
            X = [x.persistence_diagram[dim] for x in data1]
            Y = [y.persistence_diagram[dim] for y in data2]
            k_matrix = w * k.fit(X).transform(Y)
            kernel_matrices.append(k_matrix)

        return sum(kernel_matrices)

    return kernel_wrapper


class PersistenceKernelSVM(BaseEstimator, ClassifierMixin):
    """
    A sklearn compatible SVM classifier that computes persistent homology based kernels

    :param kernel_type: type of persistence kernel to use, should be one of 'scale space',
        'weighted gaussian', 'sliced_wasserstein' or 'fisher'. The same kernel is used
        across all homology dimensions
    :param C: traditional `C` parameter for SVMs
    :param homology_dims: the dimensions for which kernel is evaluated
    :param hdim_weights: the weights for kernels of each dimensions. Should be of same
        length as `homology_dims`
    """

    def __init__(self, kernel_type='scale_space', C=1.0, homology_dims=(0, 1), hdim_weights=(0.5, 0.5)):

        self.kernel_type = kernel_type
        self.C = C
        self.homology_dims = homology_dims
        self.hdim_weights = hdim_weights

    def fit(self, X, y):
        self.X_ = X

        # Check that weights sum to one
        if sum(self.hdim_weights) != 1:
            raise ValueError('hdim_weights = {} do not sum to 1.'.format(self.hdim_weights))

        # Get the kernel function as a sum of kernel for each homology dimension
        self.kernel_ = get_kernel(self.kernel_type, self.hdim_weights)

        self.svm = SVC(C=self.C, kernel='precomputed')
        self.svm.fit(self.kernel_(X, X), y)

        return self

    def predict(self, X):
        return self.svm.predict(self.kernel_(self.X_, X))


class DataContainer:

    def __init__(self, data):
        self.X_train = data[0]
        self.X_test  = data[1]
        self.y_train = data[2]
        self.y_test  = data[3]

    def __repr__(self):
        return 'Train size={}, Test size={}'.format(len(self.X_train), len(self.X_test))


class Model:
    def __init__(self, model, model_name: str, feature_extractor):
        self.model = model
        self.model_name = model_name
        self.feature_extractor = feature_extractor
        self._score = None
        self.train_time = None

    def fit(self, X, y):
        start = time()
        self.model.fit(self.feature_extractor(X), y)
        elapsed = time() - start
        self.train_time = elapsed

    def predict(self, X):
        return self.model.predict(self.feature_extractor(X))

    def score(self, X, y, metric):
        self._score = metric(y, self.model.predict(self.feature_extractor(X)))
        return self._score

    def __repr__(self):
        return str(self.model)


class ModelManager:

    def __init__(self, persist_dir: str, dataset: DataContainer, overwrite=False):
        """
        Manage Sklearn compatible models. Handles dataset storage, model training, evaluating test metrics,
        model comparison and generating performance reports

        :param persist_dir: directory where models are persisted to disk for future evaluation
        :param dataset: DataContainer object that contains the train and test split
        :param overwrite: if True, allows overwriting another model with same model name.
            If False, trying to overwrite same model raises a KeyError.
        """

        self.persist_dir = persist_dir
        self.dataset = dataset
        self.overwrite = overwrite
        self.models = {}

    def add_model(self, new_model, new_model_name, feature_extractor):
        """
        Add a new model object to the model manager

        :param new_model: sklearn compatible model object
        :param new_model_name: model name
        :param feature_extractor: function that extracts the appropriate features
        """

        new_model_obj = Model(new_model, new_model_name, feature_extractor)

        if not self.overwrite and new_model_name in self.models:
            raise KeyError('"{}" already in ModelManger.'.format(new_model_name))
        else:
            self.models[new_model_name] = new_model_obj

    def remove_model(self, model_name):
        """
        Remove a model from the manager

        :param model_name: name of the model to be removed
        """
        if model_name not in self.models:
            raise KeyError('"{}" not found.'.format(model_name))
        else:
            del self.models[model_name]

    def train(self, model_name):
        """
        Train model with given name

        :param model_name: name of the model
        """

        if model_name not in self.models:
            raise KeyError('Model name = "{}" not found in ModelManager.'.format(model_name))

        model = self.models[model_name]

        # X_train = model.feature_extractor(self.dataset.X_train)

        model.fit(self.dataset.X_train, self.dataset.y_train)

    def evaluate(self, model_name: str, metric):
        """
        Evaluate model w.r.t specified metric

        :param model_name: name of the model
        :param metric: function of form f(ground_truth, prediction) that returns a scalar value
            such as accuracy, ground_truth and prediction can be lists or numpy arrays
        :return: value of evaluated metric, also updates the Model object with the score
        """

        model: Model = self.models[model_name]

        # X_test = model.feature_extractor(self.dataset.X_test)

        return model.score(self.dataset.X_test, self.dataset.y_test, metric)

    def train_all(self, subset=None):
        prog_bar = tqdm(self.models)

        for model_name in prog_bar:
            if subset is not None and model_name not in subset:
                    continue
            prog_bar.set_description('Model = {}'.format(model_name))
            self.train(model_name)

    def evaluate_all(self, metric):
        for model_name in self.models:
            self.evaluate(model_name, metric)

    def tabulate(self):
        table = []

        for model_name, model in self.models.items():
            table.append([model_name, model.train_time, model._score])

        return pd.DataFrame(table, columns=['Model', 'Train time', 'Score'])

    def mcnemar_test(self, model_name1: str, model_name2: str):
        model1: Model = self.models[model_name1]
        model2: Model = self.models[model_name2]
        ytest = self.dataset.y_test

        y_pred1 = model1.predict(self.dataset.X_test)
        y_pred2 = model2.predict(self.dataset.X_test)

        ground_truth = self.dataset.y_test

        contingency_table = contingency_matrix(y_pred1 == ground_truth, y_pred2 == ground_truth)

        print(statsmodels.stats.contingency_tables.mcnemar(contingency_table))
