"""Tests for inter-rater agreement and judge calibration diagnostics."""

from __future__ import annotations

import pytest

from valkit.errors import AcceptanceError
from valkit.stats.agreement import (
    cohen_kappa,
    confusion_matrix,
    kappa_confidence_interval,
    kappa_diagnostics,
    kappa_standard_error,
    percent_agreement,
    summarise_agreement,
    weighted_kappa,
)


def _classic_pair():
    """The textbook 2x2 example: 50 items, both-yes 20, A-only 5, B-only 10, both-no 15.

    Observed agreement 0.70, chance agreement 0.50, so kappa = 0.40.
    """
    a = ["y"] * 20 + ["y"] * 5 + ["n"] * 10 + ["n"] * 15
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    return a, b


class TestCohenKappa:
    def test_worked_example(self):
        a, b = _classic_pair()
        assert percent_agreement(a, b) == pytest.approx(0.70)
        assert cohen_kappa(a, b) == pytest.approx(0.40, abs=1e-12)

    def test_computed_by_hand_from_the_definition(self):
        """Recompute from (po - pe) / (1 - pe) rather than trusting the function."""
        a, b = _classic_pair()
        n = len(a)
        po = sum(1 for x, y in zip(a, b) if x == y) / n
        pe = sum(
            (a.count(c) / n) * (b.count(c) / n) for c in {"y", "n"}
        )
        assert cohen_kappa(a, b) == pytest.approx((po - pe) / (1 - pe), abs=1e-15)

    def test_perfect_agreement(self):
        assert cohen_kappa(["a", "b", "a", "c"], ["a", "b", "a", "c"]) == 1.0

    def test_total_disagreement_is_negative(self):
        assert cohen_kappa(["a", "b", "a", "b"], ["b", "a", "b", "a"]) < 0.0

    def test_chance_level_agreement_is_about_zero(self):
        a = ["y", "n"] * 50
        b = ["y", "y", "n", "n"] * 25
        assert abs(cohen_kappa(a, b)) < 0.05

    def test_degenerate_single_category_agreeing(self):
        """Chance agreement is total; the documented convention returns 1.0."""
        assert cohen_kappa([1, 1, 1], [1, 1, 1]) == 1.0

    def test_degenerate_single_category_disagreeing(self):
        assert cohen_kappa([1, 1, 1], [0, 0, 0]) == 0.0

    def test_is_symmetric_in_its_raters(self):
        a, b = _classic_pair()
        assert cohen_kappa(a, b) == pytest.approx(cohen_kappa(b, a), abs=1e-15)

    def test_mismatched_lengths_rejected(self):
        with pytest.raises(AcceptanceError, match="same length"):
            cohen_kappa([1, 2, 3], [1, 2])

    def test_empty_rejected(self):
        with pytest.raises(AcceptanceError, match="empty sample"):
            cohen_kappa([], [])


class TestWeightedKappa:
    def test_identical_ordinal_scores(self):
        assert weighted_kappa([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0

    def test_near_misses_penalised_less_than_far_misses(self):
        reference = [1, 2, 3, 4, 5] * 4
        near = [1, 2, 3, 4, 4] * 4
        far = [1, 2, 3, 4, 1] * 4
        assert weighted_kappa(reference, near) > weighted_kappa(reference, far)

    def test_quadratic_penalises_distance_more_sharply(self):
        reference = [1, 2, 3, 4, 5] * 4
        prediction = [1, 2, 3, 4, 1] * 4
        linear = weighted_kappa(reference, prediction, weights="linear")
        quadratic = weighted_kappa(reference, prediction, weights="quadratic")
        assert quadratic < linear

    def test_invalid_weight_scheme(self):
        with pytest.raises(AcceptanceError, match="linear"):
            weighted_kappa([1, 2], [1, 2], weights="cubic")


class TestConfusionMatrix:
    def test_cells_assigned_to_the_right_corners(self):
        # reference (human) then prediction (judge)
        counts = confusion_matrix([1, 1, 0, 0, 1], [1, 0, 0, 1, 1])
        assert counts == {"tp": 2, "fn": 1, "tn": 1, "fp": 1}

    def test_false_positive_is_a_judge_pass_the_human_failed(self):
        counts = confusion_matrix(reference=[0], prediction=[1])
        assert counts["fp"] == 1
        assert counts["fn"] == 0

    def test_argument_order_is_not_symmetric(self):
        forward = confusion_matrix([1, 0], [0, 0])
        reverse = confusion_matrix([0, 0], [1, 0])
        assert forward["fn"] == 1 and forward["fp"] == 0
        assert reverse["fp"] == 1 and reverse["fn"] == 0

    def test_accepts_bools_and_floats(self):
        assert confusion_matrix([True, False], [1.0, 0.0]) == {
            "tp": 1,
            "fp": 0,
            "tn": 1,
            "fn": 0,
        }

    def test_counts_sum_to_n(self):
        reference = [1, 0, 1, 1, 0, 0, 1]
        prediction = [1, 1, 1, 0, 0, 0, 1]
        counts = confusion_matrix(reference, prediction)
        assert sum(counts.values()) == len(reference)


class TestStandardErrorAndInterval:
    def test_interval_brackets_the_estimate(self):
        a, b = _classic_pair()
        kappa = cohen_kappa(a, b)
        low, high = kappa_confidence_interval(a, b, 0.95)
        assert low < kappa < high

    def test_more_data_narrows_the_interval(self):
        a, b = _classic_pair()
        narrow = kappa_confidence_interval(a * 10, b * 10, 0.95)
        wide = kappa_confidence_interval(a, b, 0.95)
        assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])

    def test_standard_error_is_zero_under_total_chance_agreement(self):
        assert kappa_standard_error([1, 1, 1], [1, 1, 1]) == 0.0

    def test_interval_clamped_to_the_valid_range(self):
        low, high = kappa_confidence_interval(["a"] * 20 + ["b"] * 20, ["a"] * 20 + ["b"] * 20)
        assert low >= -1.0 and high <= 1.0


class TestDiagnostics:
    def test_prevalence_caveat_on_skewed_labels(self):
        reference = [1] * 45 + [0] * 5
        prediction = [1] * 44 + [0] + [0] * 4 + [1]
        _, _, caveats = kappa_diagnostics(reference, prediction)
        assert any("prevalence" in c.lower() for c in caveats)

    def test_no_prevalence_caveat_on_balanced_labels(self):
        reference = [1] * 25 + [0] * 25
        prediction = [1] * 24 + [0] + [0] * 24 + [1]
        _, _, caveats = kappa_diagnostics(reference, prediction)
        assert not any("prevalence" in c.lower() for c in caveats)

    def test_false_pass_caveat_is_raised(self):
        """The judge passing cases the human failed is the direction that matters."""
        reference = [1] * 20 + [0] * 10
        prediction = [1] * 20 + [1] * 5 + [0] * 5
        _, _, caveats = kappa_diagnostics(reference, prediction)
        assert any("passed" in c and "human" in c for c in caveats)

    def test_single_class_reference_is_flagged_as_uninformative(self):
        _, _, caveats = kappa_diagnostics([1] * 20, [1] * 19 + [0])
        assert any("only one class" in c for c in caveats)

    def test_bias_index_direction(self):
        prevalence, bias, _ = kappa_diagnostics([1, 1, 0, 0], [1, 1, 1, 1])
        assert bias > 0  # more false passes than false failures

    def test_prevalence_index_is_zero_on_balanced_perfect_agreement(self):
        prevalence, bias, _ = kappa_diagnostics([1, 1, 0, 0], [1, 1, 0, 0])
        assert prevalence == 0.0
        assert bias == 0.0


class TestSummary:
    def test_summary_reports_the_pathology_it_documents(self):
        """96% agreement on a skewed set still yields a middling kappa."""
        reference = [1] * 45 + [0] * 5
        prediction = [1] * 44 + [0] + [0] * 4 + [1]
        summary = summarise_agreement(reference, prediction)
        assert summary.n == 50
        assert summary.percent_agreement == pytest.approx(0.96)
        assert 0.7 < summary.kappa < 0.85
        assert summary.interpretation == "substantial"
        assert summary.caveats, "a skewed set must carry a caveat"

    def test_summary_confusion_is_consistent_with_n(self):
        summary = summarise_agreement([1, 0, 1, 1], [1, 1, 1, 0])
        assert sum(summary.confusion.values()) == summary.n

    def test_perfect_agreement_summary(self):
        summary = summarise_agreement([1, 0] * 25, [1, 0] * 25)
        assert summary.kappa == 1.0
        assert summary.interpretation == "almost perfect"
        assert summary.percent_agreement == 1.0
