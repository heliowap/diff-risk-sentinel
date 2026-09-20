import unittest
from evals.review_cost.models import Finding, CaseConfig
from evals.review_cost.grader import grade_case_findings, match_function_name


class TestGrader(unittest.TestCase):

    def test_match_function_name(self):
        self.assertTrue(match_function_name("Service.process", ["Service.process"]))
        self.assertTrue(match_function_name("process", ["Service.process"]))
        self.assertTrue(match_function_name("pkg.module.Service.process", ["Service.process"]))
        self.assertFalse(match_function_name("other_func", ["Service.process"]))

    def test_grade_clean_case(self):
        case = CaseConfig(
            case_id="clean_01",
            intro_commit="c0",
            base_commit="b0",
            category="clean",
            touched_production_functions=10,
            fix_commit=None,
            fixed_functions=[],
        )
        findings = [
            Finding(file="a.py", line=10, function="foo", claim="Possible None dereference", severity="major")
        ]
        grading = grade_case_findings(case, findings, fix_diff="", fix_message="")
        # On a clean commit, known defect is missed by definition (no known defect)
        self.assertEqual(grading.known_defect_verdict, "missed")
        self.assertEqual(len(grading.finding_judgments), 1)

    def test_grade_defect_case_missed(self):
        case = CaseConfig(
            case_id="med_01",
            intro_commit="c0",
            base_commit="b0",
            category="medium",
            touched_production_functions=40,
            fix_commit="fix0",
            fixed_functions=["OrderService.checkout"],
        )
        findings = [
            Finding(file="a.py", line=10, function="User.get_name", claim="Unused var", severity="minor")
        ]
        grading = grade_case_findings(case, findings, fix_diff="", fix_message="fix null pointer in checkout")
        self.assertEqual(grading.known_defect_verdict, "missed")

    def test_grade_defect_case_matched_function(self):
        case = CaseConfig(
            case_id="med_02",
            intro_commit="c0",
            base_commit="b0",
            category="medium",
            touched_production_functions=40,
            fix_commit="fix0",
            fixed_functions=["OrderService.checkout"],
        )
        findings = [
            Finding(
                file="orders.py",
                line=55,
                function="OrderService.checkout",
                claim="checkout throws AttributeError when user has no billing address",
                severity="critical",
            )
        ]
        grading = grade_case_findings(
            case,
            findings,
            fix_diff="""--- a/orders.py
+++ b/orders.py
@@ -55,2 +55,4 @@
+    if not user.billing_address:
+        raise ValueError("Missing billing address")
""",
            fix_message="fix: handle missing billing address in checkout",
        )
        # The function matches and keywords align with fix message
        self.assertIn(grading.known_defect_verdict, ("found", "near"))

    def test_grade_defect_case_matched_dict_target(self):
        case = CaseConfig(
            case_id="dict_01",
            intro_commit="c0",
            base_commit="b0",
            category="small",
            touched_production_functions=10,
            fix_commit="fix0",
            fixed_functions={
                "src/components/Inbox.tsx": ["Inbox.refreshMessages"],
            },
        )
        findings = [
            Finding(
                file="src/components/Inbox.tsx",
                line=30,
                function="Inbox.refreshMessages",
                claim="Missing check for active channel before reload",
                severity="major",
            )
        ]
        grading = grade_case_findings(
            case,
            findings,
            fix_diff="diff",
            fix_message="fix reload of replies",
        )
        self.assertEqual(grading.known_defect_verdict, "found")
        self.assertEqual(grading.finding_judgments[0].verdict, "correct")

