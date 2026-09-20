import unittest
from evals.review_cost.select_cases import stratify_and_select_cases


class TestSelectCases(unittest.TestCase):

    def test_stratification(self):
        # Create synthetic dataset with small, medium, large, and clean cases
        raw_cases = []
        # 10 small
        for i in range(10):
            raw_cases.append({
                "intro": f"intro_s_{i}",
                "fix": f"fix_s_{i}",
                "fixed_functions": ["foo"],
                "touched_production_functions": 10 + i,
                "subject": f"feat small {i}",
            })
        # 10 medium
        for i in range(10):
            raw_cases.append({
                "intro": f"intro_m_{i}",
                "fix": f"fix_m_{i}",
                "fixed_functions": ["bar"],
                "touched_production_functions": 40 + i * 5,
                "subject": f"feat medium {i}",
            })
        # 10 large
        for i in range(10):
            raw_cases.append({
                "intro": f"intro_l_{i}",
                "fix": f"fix_l_{i}",
                "fixed_functions": ["baz"],
                "touched_production_functions": 160 + i * 10,
                "subject": f"feat large {i}",
            })

        clean_commits = [
            {"intro": f"clean_{i}", "subject": f"clean {i}", "touched_production_functions": 20 + i}
            for i in range(8)
        ]

        selected = stratify_and_select_cases(
            cases=raw_cases,
            clean_commits=clean_commits,
            n_small=8,
            n_medium=8,
            n_large=8,
            n_clean=6,
            seed=42,
            excluded_intros={"intro_s_0", "intro_m_0"},
        )

        self.assertEqual(len(selected), 8 + 8 + 8 + 6)
        by_cat = {}
        for c in selected:
            by_cat.setdefault(c.category, []).append(c)

        self.assertEqual(len(by_cat["small"]), 8)
        self.assertEqual(len(by_cat["medium"]), 8)
        self.assertEqual(len(by_cat["large"]), 8)
        self.assertEqual(len(by_cat["clean"]), 6)

        # Excluded intros must not be present
        selected_intros = {c.intro_commit for c in selected}
        self.assertNotIn("intro_s_0", selected_intros)
        self.assertNotIn("intro_m_0", selected_intros)

    def test_excluded_intro_prefix_removes_case(self):
        cases = [{
            "intro": "abcdef1234567890",
            "fix": "fedcba0987654321",
            "intro_subject": "feat: add behavior",
            "fix_subject": "fix: correct behavior",
            "fixed_functions": {"src/example.py": ["calculate"]},
            "touched_production_functions": 3,
        }]
        selected = stratify_and_select_cases(
            cases,
            n_small=1,
            n_medium=0,
            n_large=0,
            n_clean=0,
            excluded_intros={"abcdef12"},
        )
        self.assertEqual(selected, [])
