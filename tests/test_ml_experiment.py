"""Phase 8 tests use synthetic chronological data only."""

from dataclasses import replace
from decimal import Decimal
import json

import pytest

from config.settings import Settings
from evaluation.ml_experiment import SplitConfig, run_logistic_experiment
from evaluation.ml_reporting import experiment_to_dict, write_experiment_report
from prediction.features import FeatureConfig, build_feature_samples, feature_vector


def synthetic_values(repeats: int = 30) -> list[str]:
    pattern = ["1.1", "1.6", "2.2", "6.0", "1.2", "3.0"]
    return pattern * repeats


def test_feature_vector_excludes_target_value() -> None:
    config = FeatureConfig(lag_count=2, rolling_window=5)
    common_history = ["1", "2", "3", "4", "5"]
    low_target = build_feature_samples(common_history + ["1"], config)[0]
    high_target = build_feature_samples(common_history + ["100"], config)[0]
    assert low_target.target_index == high_target.target_index == 5
    assert low_target.values == high_target.values
    assert low_target.values == feature_vector(common_history, config)


def test_feature_rows_are_chronological_and_warmup_bounded() -> None:
    config = FeatureConfig(lag_count=3, rolling_window=5)
    samples = build_feature_samples(["1", "2", "3", "4", "5", "6", "7"], config)
    assert [sample.target_index for sample in samples] == [5, 6]
    assert all(len(sample.values) == len(config.feature_names) for sample in samples)


@pytest.mark.parametrize("kwargs", [
    {"lag_count": 0},
    {"lag_count": 4, "rolling_window": 3},
    {"rate_thresholds": ()},
    {"rate_thresholds": ("2", "1.5")},
])
def test_invalid_feature_config_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        FeatureConfig(**kwargs)


@pytest.mark.parametrize("kwargs", [
    {"validation_fraction": 0},
    {"test_fraction": 1},
    {"validation_fraction": 0.6, "test_fraction": 0.4},
])
def test_invalid_split_config_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        SplitConfig(**kwargs)


def test_logistic_experiment_uses_final_chronological_test_block() -> None:
    values = synthetic_values()
    features = FeatureConfig(lag_count=2, rolling_window=5)
    report = run_logistic_experiment(
        values,
        thresholds=(Decimal("1.5"), Decimal("2"), Decimal("5")),
        feature_config=features,
        split_config=SplitConfig(validation_fraction=0.2, test_fraction=0.2),
        c_values=(0.1, 1.0),
    )
    assert len(report.experiments) == 3
    for experiment in report.experiments:
        assert experiment.status == "COMPLETE"
        assert experiment.selected_c in {0.1, 1.0}
        assert experiment.test_brier is not None
        assert experiment.baseline_test_brier is not None
        assert len(experiment.test_predictions) == experiment.test_rows
        indices = [prediction.target_index for prediction in experiment.test_predictions]
        assert indices == sorted(indices)
        assert min(indices) >= features.warmup
        assert all(prediction.actual in {0, 1} for prediction in experiment.test_predictions)


def test_logistic_experiment_is_deterministic() -> None:
    values = synthetic_values()
    kwargs = dict(
        thresholds=(Decimal("2"),),
        feature_config=FeatureConfig(lag_count=2, rolling_window=5),
        c_values=(0.1, 1.0),
    )
    first = run_logistic_experiment(values, **kwargs)
    second = run_logistic_experiment(values, **kwargs)
    assert first == second
    assert len(first.dataset_fingerprint) == 64


def test_training_single_class_is_reported_without_fabricating_score() -> None:
    report = run_logistic_experiment(
        ["1.1"] * 40,
        thresholds=(Decimal("2"),),
        feature_config=FeatureConfig(lag_count=2, rolling_window=5),
    )
    experiment = report.experiments[0]
    assert experiment.status == "INSUFFICIENT_CLASS_VARIATION"
    assert experiment.test_brier is None
    assert experiment.baseline_test_brier is None
    assert experiment.test_predictions == ()


def test_saved_report_contains_reproducible_test_predictions(
    settings: Settings,
) -> None:
    report = run_logistic_experiment(
        synthetic_values(),
        thresholds=(Decimal("2"),),
        feature_config=FeatureConfig(lag_count=2, rolling_window=5),
        c_values=(1.0,),
    )
    document = experiment_to_dict(report)
    assert document["report_version"] == 1
    assert document["model_name"] == "logistic-threshold-classifier"
    assert document["experiments"][0]["test_predictions"]

    path = write_experiment_report(report, settings, "fixture-experiment.json")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == document
    with pytest.raises(FileExistsError):
        write_experiment_report(report, settings, "fixture-experiment.json")


def test_future_change_cannot_modify_earlier_feature_row() -> None:
    config = FeatureConfig(lag_count=2, rolling_window=5)
    original = synthetic_values(3)
    changed = original.copy()
    changed[-1] = "999"
    original_samples = build_feature_samples(original, config)
    changed_samples = build_feature_samples(changed, config)
    # Every row before the changed final target must remain identical.
    assert original_samples[:-1] == changed_samples[:-1]
