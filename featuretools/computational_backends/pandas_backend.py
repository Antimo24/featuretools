import cProfile
import cStringIO
import logging
import os
import pstats
import sys
import uuid
import warnings
from datetime import datetime

import numpy as np
import pandas as pd

# featuretools
from .base_backend import ComputationalBackend
from .feature_tree import FeatureTree

from featuretools import variable_types
from featuretools.entityset.relationship import Relationship
from featuretools.exceptions import UnknownFeature
from featuretools.primitives import (
    AggregationPrimitive,
    DirectFeature,
    IdentityFeature,
    TransformPrimitive
)
# progress bar
from featuretools.utils.gen_utils import make_tqdm_iterator

warnings.simplefilter('ignore', np.RankWarning)
warnings.simplefilter("ignore", category=RuntimeWarning)
logger = logging.getLogger('featuretools.computational_backend')
ROOT_DIR = os.path.expanduser("~")


class PandasBackend(ComputationalBackend):

    def __init__(self, entityset, features):
        assert len(set(f.entity.id for f in features)) == 1, \
            "Features must all be defined on the same entity"

        self.entityset = entityset
        self.target_eid = features[0].entity.id
        self.features = features
        self.feature_tree = FeatureTree(entityset, features)

    def calculate_all_features(self, instance_ids, time_last,
                               training_window=None, profile=False,
                               precalculated_features=None, ignored=None,
                               verbose=False):
        """
        Given a list of instance ids and features with a shared time window,
        generate and return a mapping of instance -> feature values.

        Args:
            instance_ids (list): list of instance id to build features for

            time_last (pd.Timestamp): last allowed time. Data from exactly this
                time not allowed

            training_window (:class:Timedelta, optional): Data older than
                time_last by more than this will be ignored

            profile (boolean): enable profiler if True

            verbose (boolean): print output progress if True

        Returns:
            pd.DataFrame : Pandas DataFrame of calculated feature values.
                Indexed by instance_ids. Columns in same order as features
                passed in.

        """
        assert len(instance_ids) > 0, "0 instance ids provided"
        self.instance_ids = instance_ids

        self.time_last = time_last
        if self.time_last is None:
            self.time_last = datetime.now()

        # For debugging
        if profile:
            pr = cProfile.Profile()
            pr.enable()

        if precalculated_features is None:
            precalculated_features = {}
        # Access the index to get the filtered data we need
        target_entity = self.entityset[self.target_eid]
        if ignored:
            # TODO: Just want to remove entities if don't have any (sub)features defined
            # on them anymore, rather than recreating
            ordered_entities = FeatureTree(self.entityset, self.features, ignored=ignored).ordered_entities
        else:
            ordered_entities = self.feature_tree.ordered_entities
        eframes_by_filter = \
            self.entityset.get_pandas_data_slice(filter_entity_ids=ordered_entities,
                                                 index_eid=self.target_eid,
                                                 instances=instance_ids,
                                                 time_last=time_last,
                                                 training_window=training_window,
                                                 verbose=verbose)

        # Handle an empty time slice by returning a dataframe with defaults
        if eframes_by_filter is None:
            return self.generate_default_df(instance_ids=instance_ids)

        finished_entity_ids = []
        # Populate entity_frames with precalculated features
        if len(precalculated_features) > 0:
            for entity_id, precalc_feature_values in precalculated_features.items():
                if entity_id in eframes_by_filter:
                    frame = eframes_by_filter[entity_id][entity_id]
                    eframes_by_filter[entity_id][entity_id] = pd.merge(frame,
                                                                       precalc_feature_values,
                                                                       left_index=True,
                                                                       right_index=True)
                else:
                    # Only features we're taking from this entity
                    # are precomputed
                    # Make sure the id variable is a column as well as an index
                    entity_id_var = self.entityset[entity_id].index
                    precalc_feature_values[entity_id_var] = precalc_feature_values.index.values
                    eframes_by_filter[entity_id] = {entity_id: precalc_feature_values}
                    finished_entity_ids.append(entity_id)

        # Iterate over the top-level entities (filter entities) in sorted order
        # and calculate all relevant features under each one.

        if verbose:
            total_groups_to_compute = sum(len(group)
                                          for group in self.feature_tree.ordered_feature_groups.values())

            pbar = make_tqdm_iterator(total=total_groups_to_compute,
                                      desc="Computing features",
                                      unit="feature group")
            if verbose:
                pbar.update(0)

        for filter_eid in ordered_entities:
            entity_frames = eframes_by_filter[filter_eid]

            # update the current set of entity frames with the computed features
            # from previously finished entities
            for eid in finished_entity_ids:
                # only include this frame if it's not from a descendent entity:
                # descendent entity frames will have to be re-calculated.
                # TODO: this check might not be necessary, depending on our
                # constraints
                if not self.entityset.find_backward_path(start_entity_id=filter_eid,
                                                         goal_entity_id=eid):
                    entity_frames[eid] = eframes_by_filter[eid][eid]

            if filter_eid in self.feature_tree.ordered_feature_groups:
                for group in self.feature_tree.ordered_feature_groups[filter_eid]:
                    if verbose:

                        pbar.set_postfix({'running': 0})

                    handler = self._feature_type_handler(group[0])
                    handler(group, entity_frames)

                    if verbose:
                        pbar.update(1)

            finished_entity_ids.append(filter_eid)

        if verbose:
            pbar.set_postfix({'running': 0})
            pbar.refresh()
            sys.stdout.flush()
            pbar.close()

        # debugging
        if profile:
            pr.disable()
            s = cStringIO.StringIO()
            ps = pstats.Stats(pr, stream=s).sort_stats("cumulative", "tottime")
            ps.print_stats()
            prof_folder_path = os.path.join(ROOT_DIR, 'prof')
            if not os.path.exists(prof_folder_path):
                os.mkdir(prof_folder_path)
            with open(os.path.join(prof_folder_path, 'inst-%s.log' %
                                   list(instance_ids)[0]), 'w') as f:
                f.write(s.getvalue())

        df = eframes_by_filter[self.target_eid][self.target_eid]

        # fill in empty rows with default values
        missing_ids = [i for i in instance_ids if i not in
                       df[target_entity.index]]
        if missing_ids:
            df = df.append(self.generate_default_df(instance_ids=missing_ids,
                                                    extra_columns=df.columns))
        return df[[feat.get_name() for feat in self.features]]

    def generate_default_df(self, instance_ids, extra_columns=None):
        index_name = self.features[0].entity.index
        default_row = [f.default_value for f in self.features]
        default_cols = [f.get_name() for f in self.features]
        default_matrix = [default_row] * len(instance_ids)
        default_df = pd.DataFrame(default_matrix,
                                  columns=default_cols,
                                  index=instance_ids)
        default_df.index.name = index_name
        if extra_columns is not None:
            for c in extra_columns:
                if c not in default_df.columns:
                    default_df[c] = [np.nan] * len(instance_ids)
        return default_df

    def _feature_type_handler(self, f):
        if isinstance(f, TransformPrimitive):
            return self._calculate_transform_features
        elif isinstance(f, DirectFeature):
            return self._calculate_direct_features
        elif isinstance(f, AggregationPrimitive):
            return self._calculate_agg_features
        elif isinstance(f, IdentityFeature):
            return self._calculate_identity_features
        else:
            raise UnknownFeature(u"{} feature unknown".format(f.__class__))

    def _calculate_identity_features(self, features, entity_frames):
        entity_id = features[0].entity.id
        assert entity_id in entity_frames and features[0].get_name() in entity_frames[entity_id].columns

    def _calculate_transform_features(self, features, entity_frames):
        entity_id = features[0].entity.id
        assert len(set([f.entity.id for f in features])) == 1, \
            "features must share base entity"
        assert entity_id in entity_frames

        frame = entity_frames[entity_id]
        for f in features:
            # handle when no data
            if frame.shape[0] == 0:
                set_default_column(frame, f)
                continue

            # collect only the variables we need for this transformation
            variable_data = [frame[bf.get_name()].values
                             for bf in f.base_features]

            feature_func = f.get_function()
            # apply the function to the relevant dataframe slice and add the
            # feature row to the results dataframe.
            if f.uses_calc_time:
                values = feature_func(*variable_data, time=self.time_last)
            else:
                values = feature_func(*variable_data)

            if isinstance(values, pd.Series):
                values = values.values
            frame[f.get_name()] = list(values)

        entity_frames[entity_id] = frame

    def _calculate_direct_features(self, features, entity_frames):
        entity_id = features[0].entity.id
        parent_entity_id = features[0].parent_entity.id

        assert entity_id in entity_frames and parent_entity_id in entity_frames

        path = self.entityset.find_forward_path(entity_id, parent_entity_id)
        assert len(path) == 1, \
            "Error calculating DirectFeatures, len(path) > 1"

        parent_df = entity_frames[parent_entity_id]
        child_df = entity_frames[entity_id]
        merge_var = path[0].child_variable.id

        # generate a mapping of old column names (in the parent entity) to
        # new column names (in the child entity) for the merge
        col_map = {path[0].parent_variable.id: merge_var}
        index_as_feature = None
        for f in features:
            if f.base_features[0].get_name() == path[0].parent_variable.id:
                index_as_feature = f
            # Sometimes entityset._add_multigenerational_links adds link variables
            # that would ordinarily get calculated as direct features,
            # so we make sure not to attempt to calculate again
            if f.get_name() in child_df.columns:
                continue
            col_map[f.base_features[0].get_name()] = f.get_name()

        # merge the identity feature from the parent entity into the child
        merge_df = parent_df[col_map.keys()].rename(columns=col_map)
        if index_as_feature is not None:
            merge_df.set_index(index_as_feature.get_name(), inplace=True, drop=False)
        else:
            merge_df.set_index(merge_var, inplace=True)

        new_df = pd.merge(left=child_df, right=merge_df,
                          left_on=merge_var, right_index=True,
                          how='left')

        entity_frames[entity_id] = new_df

    def _calculate_agg_features(self, features, entity_frames):
        test_feature = features[0]
        use_previous = test_feature.use_previous
        base_features = test_feature.base_features
        where = test_feature.where
        entity = test_feature.entity
        child_entity = base_features[0].entity

        assert entity.id in entity_frames and child_entity.id in entity_frames

        index_var = entity.index
        frame = entity_frames[entity.id]
        base_frame = entity_frames[child_entity.id]
        # Sometimes approximate features get computed in a previous filter frame
        # and put in the current one dynamically,
        # so there may be existing features here
        features = [f for f in features if f.get_name()
                    not in frame.columns]
        if not len(features):
            return

        # handle where clause for all functions below
        if where is not None:
            base_frame = base_frame[base_frame[where.get_name()]]

        relationship_path = self.entityset.find_backward_path(entity.id,
                                                              child_entity.id)

        groupby_var = Relationship._get_link_variable_name(relationship_path)

        # if the use_previous property exists on this feature, include only the
        # instances from the child entity included in that Timedelta
        if use_previous and not base_frame.empty:
            # Filter by use_previous values
            time_last = self.time_last
            if use_previous.is_absolute():
                time_first = time_last - use_previous
                ti = child_entity.time_index
                if ti is not None:
                    base_frame = base_frame[base_frame[ti] >= time_first]
            else:
                n = use_previous.value

                def last_n(df):
                    return df.iloc[-n:]

                base_frame = base_frame.groupby(groupby_var).apply(last_n)

        if not base_frame.empty:
            if groupby_var not in base_frame:
                # This occured sometimes. I think it might have to do with category
                # but not sure. TODO: look into when this occurs
                no_instances = True
            # if the foreign key column in the child (base_frame) that links to
            # frame is an integer and the id column in the parent is an object or
            # category dtype, the .isin() call errors.
            elif (frame[index_var].dtype != base_frame[groupby_var].dtype or
                    frame[index_var].dtype.name.find('category') > -1):
                try:
                    frame_as_obj = frame[index_var].astype(object)
                    base_frame_as_obj = base_frame[groupby_var].astype(object)
                except ValueError:
                    msg = u"Could not join {}.{} (dtype={}) with {}.{} (dtype={})"
                    raise ValueError(msg.format(entity.id, index_var,
                                                frame[index_var].dtype,
                                                child_entity.id, groupby_var,
                                                base_frame[groupby_var].dtype))
                else:
                    no_instances = check_no_related_instances(frame_as_obj.values, base_frame_as_obj.values)
            else:
                no_instances = check_no_related_instances(frame[index_var].values, base_frame[groupby_var].values)

        if base_frame.empty or no_instances:
            for f in features:
                set_default_column(entity_frames[entity.id], f)

            return

        def wrap_func_with_name(func, name):
            def inner(x):
                return func(x)
            inner.__name__ = name
            return inner

        to_agg = {}
        agg_rename = {}
        to_apply = set()
        # apply multivariable and time-dependent features as we find them, and
        # save aggregable features for later
        for f in features:
            if _can_agg(f):
                variable_id = f.base_features[0].get_name()
                if variable_id not in to_agg:
                    to_agg[variable_id] = []
                func = f.get_function()
                # make sure function names are unique
                random_id = str(uuid.uuid1())
                func = wrap_func_with_name(func, random_id)
                funcname = random_id
                to_agg[variable_id].append(func)
                agg_rename[u"{}-{}".format(variable_id, funcname)] = f.get_name()

                continue

            to_apply.add(f)

        # Apply the non-aggregable functions generate a new dataframe, and merge
        # it with the existing one
        if len(to_apply):
            wrap = agg_wrapper(to_apply, self.time_last)
            # groupby_var can be both the name of the index and a column,
            # to silence pandas warning about ambiguity we explicitly pass
            # the column (in actuality grouping by both index and group would
            # work)
            to_merge = base_frame.groupby(base_frame[groupby_var]).apply(wrap)

            to_merge.reset_index(1, drop=True, inplace=True)
            frame = pd.merge(left=frame, right=to_merge,
                             left_on=index_var, right_index=True, how='left')

        # Apply the aggregate functions to generate a new dataframe, and merge
        # it with the existing one
        # Do the [variables] accessor on to_merge because the agg call returns
        # a dataframe with columns that contain the dataframes we want
        if len(to_agg):
            # groupby_var can be both the name of the index and a column,
            # to silence pandas warning about ambiguity we explicitly pass
            # the column (in actuality grouping by both index and group would
            # work)

            to_merge = base_frame.groupby(base_frame[groupby_var]).agg(to_agg)
            # we apply multiple functions to each column, creating
            # a multiindex as the column
            # rename the columns to a concatenation of the two indexes
            to_merge.columns = [u"{}-{}".format(n1, n2)
                                for n1, n2 in to_merge.columns.ravel()]
            # to enable a rename
            to_merge = to_merge.rename(columns=agg_rename)
            variables = agg_rename.values()
            to_merge = to_merge[variables]
            frame = pd.merge(left=frame, right=to_merge,
                             left_on=index_var, right_index=True, how='left')

        # Handle default values
        # 1. handle non scalar default values
        iterfeats = [f for f in features
                     if hasattr(f.default_value, '__iter__')]
        for f in iterfeats:
            nulls = pd.isnull(frame[f.get_name()])
            for ni in nulls[nulls].index:
                frame.at[ni, f.get_name()] = f.default_value

        # 2. handle scalars default values
        fillna_dict = {f.get_name(): f.default_value for f in features
                       if f not in iterfeats}
        frame.fillna(fillna_dict, inplace=True)

        # convert boolean dtypes to floats as appropriate
        # pandas behavior: https://github.com/pydata/pandas/issues/3752
        for f in features:
            if (not f.expanding and f.variable_type == variable_types.Numeric and
                    frame[f.get_name()].dtype.name in ['object', 'bool']):
                frame[f.get_name()] = frame[f.get_name()].astype(float)

        entity_frames[entity.id] = frame


def _can_agg(feature):
    assert isinstance(feature, AggregationPrimitive)
    base_features = feature.base_features
    if feature.where is not None:
        base_features = [bf.get_name() for bf in base_features
                         if bf.get_name() != feature.where.get_name()]

    if feature.uses_calc_time:
        return False

    return len(base_features) == 1 and not feature.expanding


def agg_wrapper(feats, time_last):
    def wrap(df):
        d = {}
        for f in feats:
            func = f.get_function()
            variable_ids = [bf.get_name() for bf in f.base_features]
            args = [df[v] for v in variable_ids]

            if f.uses_calc_time:
                d[f.get_name()] = [func(*args, time=time_last)]
            else:
                d[f.get_name()] = [func(*args)]

        return pd.DataFrame(d)
    return wrap


def check_no_related_instances(array1, array2):
    some_instances = False
    set_frame = set(array1)
    set_base_frame = set(array2)
    for s in set_frame:
        for b in set_base_frame:
            if s == b:
                some_instances = True
                break
    return not some_instances


def set_default_column(frame, f):
    default = f.default_value
    if hasattr(default, '__iter__'):
        l = frame.shape[0]
        default = [f.default_value] * l
    frame[f.get_name()] = default