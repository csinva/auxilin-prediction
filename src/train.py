import sys
from sklearn.linear_model import LogisticRegression, Lasso
from sklearn.neural_network import MLPClassifier
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier, VotingClassifier
from sklearn.tree import DecisionTreeClassifier
from sklearn.svm import SVC
from sklearn.discriminant_analysis import QuadraticDiscriminantAnalysis as QDA
import eli5
import numpy as np
from copy import deepcopy
from sklearn import metrics
from sklearn.feature_selection import SelectFromModel
from sklearn.calibration import CalibratedClassifierCV
from imblearn.over_sampling import RandomOverSampler, SMOTE
from sklearn.model_selection import KFold
import pickle as pkl

sys.path.append('lib')
from sklearn.neighbors import KNeighborsClassifier as KNN

scorers = {'balanced_accuracy': metrics.balanced_accuracy_score, 'accuracy': metrics.accuracy_score,
           'precision': metrics.precision_score, 'recall': metrics.recall_score, 'f1': metrics.f1_score,
           'roc_auc': metrics.roc_auc_score,
           'precision_recall_curve': metrics.precision_recall_curve, 'roc_curve': metrics.roc_curve}


def get_feature_importance(model, model_type, X_val, Y_val):
    if 'Calibrated' in str(type(model)):
        perm = eli5.sklearn.permutation_importance.PermutationImportance(model).fit(X_val, Y_val)
        imps = perm.feature_importances_
    elif model_type in ['dt']:
        imps = model.feature_importances_
    elif model_type in ['rf', 'irf']:
        #         imps, _ = feature_importance(model, np.array(X_val), np.transpose(np.vstack((Y_val, 1-Y_val))))
        imps = model.feature_importances_
    elif model_type == 'logistic':
        imps = model.coef_
    else:
        perm = eli5.sklearn.permutation_importance.PermutationImportance(model).fit(X_val, Y_val)
        imps = perm.feature_importances_
    return imps.squeeze()


def balance(X, y, balancing='ros', balancing_ratio=1):
    '''Balance classes in y using strategy specified by balancing
    Params
    -----
    balancing_ratio: float
        ratio of pos: neg samples
    '''
    class0 = np.sum(y == 0)
    class1 = np.sum(y == 1)
    class_max = max(class0, class1)

    if balancing_ratio >= 1:
        sample_nums = {0: int(class_max), 1: int(class_max * balancing_ratio)}
    else:
        sample_nums = {0: int(class_max / balancing_ratio), 1: int(class_max)}

    if balancing == 'none':
        return X, y

    if balancing == 'ros':
        sampler = RandomOverSampler(sampling_strategy=sample_nums, random_state=42)

    elif balancing == 'smote':
        sampler = SMOTE(sampling_strategy=sample_nums, random_state=42)

    X_r, Y_r = sampler.fit_resample(X, y)
    return X_r, Y_r


def train(df, feat_names,
          cell_nums_feature_selection, cell_nums_train,
          model_type='rf', outcome_def='y_thresh',
          balancing='ros', balancing_ratio=1, out_name='results/classify/test.pkl',
          calibrated=False, feature_selection=None,
          feature_selection_num=3, hyperparam=0, seed=42):
    '''Run training and fit models
    This will balance the data
    This will normalize the features before fitting
    
    Params
    ------
    normalize: bool
        if True, will normalize features before fitting
    cell_nums_feature_selection: list[str]
        cell names to use for feature selection
    
    '''
    np.random.seed(seed)
    X = df[feat_names]
    X = (X - X.mean()) / X.std()  # normalize the data
    y = df[outcome_def].values

    if model_type == 'rf':
        m = RandomForestClassifier(n_estimators=100)
    elif model_type == 'dt':
        m = DecisionTreeClassifier()
    elif model_type == 'logistic':
        m = LogisticRegression(solver='lbfgs')
    elif model_type == 'svm':
        h = {
            -1: 0.5,
            0: 1,
            1: 5
        }[hyperparam]
        m = SVC(C=h, gamma='scale')
    elif model_type == 'mlp2':
        h = {
            -1: (50,),
            0: (100,),
            1: (50, 50,)
        }[hyperparam]
        m = MLPClassifier(hidden_layer_sizes=h)
    elif model_type == 'gb':
        m = GradientBoostingClassifier()
    elif model_type == 'qda':
        m = QDA()
    elif model_type == 'KNN':
        m = KNN()
    elif model_type == 'irf':
        import irf
        m = irf.ensemble.wrf()
    elif model_type == 'voting_mlp+svm+rf':
        models_list = [('mlp', MLPClassifier()),
                       ('svm', SVC(gamma='scale')),
                       ('rf', RandomForestClassifier(n_estimators=100))]
        m = VotingClassifier(estimators=models_list, voting='hard')
    if calibrated:
        m = CalibratedClassifierCV(m)

    scores_cv = {s: [] for s in scorers.keys()}
    imps = {'model': [], 'imps': []}

    kf = KFold(n_splits=len(cell_nums_train))

    # feature selection on cell num 1    
    feature_selector = None
    if feature_selection is not None:
        if feature_selection == 'select_lasso':
            feature_selector_model = Lasso()
        elif feature_selection == 'select_rf':
            feature_selector_model = RandomForestClassifier()
        # select only feature_selection_num features
        feature_selector = SelectFromModel(feature_selector_model, threshold=-np.inf,
                                           max_features=feature_selection_num)
        idxs = df.cell_num.isin(cell_nums_feature_selection)
        feature_selector.fit(X[idxs], y[idxs].reshape(-1, 1))
        X = feature_selector.transform(X)
        support = np.array(feature_selector.get_support())
    else:
        support = np.ones(len(feat_names)).astype(np.bool)

    num_pts_by_fold_cv = []
    # loops over cv, where validation set order is cell_nums_train[0], ..., cell_nums_train[-1]
    for cv_idx, cv_val_idx in kf.split(cell_nums_train):
        # get sample indices
        idxs_cv = df.cell_num.isin(cell_nums_train[np.array(cv_idx)])
        idxs_val_cv = df.cell_num.isin(cell_nums_train[np.array(cv_val_idx)])
        X_train_cv, Y_train_cv = X[idxs_cv], y[idxs_cv]
        X_val_cv, Y_val_cv = X[idxs_val_cv], y[idxs_val_cv]
        num_pts_by_fold_cv.append(X_val_cv.shape[0])

        # resample training data
        X_train_r_cv, Y_train_r_cv = balance(X_train_cv, Y_train_cv, balancing, balancing_ratio)

        # fit
        m.fit(X_train_r_cv, Y_train_r_cv)

        # get preds
        preds = m.predict(X_val_cv)
        if 'svm' in model_type:
            preds_proba = preds
        else:
            preds_proba = m.predict_proba(X_val_cv)[:, 1]

        # add scores
        for s in scorers.keys():
            scorer = scorers[s]
            if 'roc' in s or 'curve' in s:
                scores_cv[s].append(scorer(Y_val_cv, preds_proba))
            else:
                scores_cv[s].append(scorer(Y_val_cv, preds))

        imps['model'].append(deepcopy(m))
        imps['imps'].append(get_feature_importance(m, model_type, X_val_cv, Y_val_cv))

    # save results
    # os.makedirs(out_dir, exist_ok=True)
    results = {'metrics': list(scorers.keys()),
               'num_pts_by_fold_cv': np.array(num_pts_by_fold_cv),
               'cv': scores_cv,
               'imps': imps,  # note this contains the model
               'feat_names': feat_names,
               'feature_selector': feature_selector,
               'feature_selection_num': feature_selection_num,
               'model_type': model_type,
               'balancing': balancing,
               'feat_names_selected': np.array(feat_names)[support],
               'calibrated': calibrated
               }
    pkl.dump(results, open(out_name, 'wb'))
