import unittest
from pathlib import Path

from pptx import Presentation


ROOT = Path(__file__).resolve().parents[1]


class TemplatePreviewTest(unittest.TestCase):
    def test_readme_embeds_template_preview_images(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        expected_images = [
            "templates/previews/template_report_html.png",
            "templates/previews/template_report_ppt_cover.png",
            "templates/previews/template_report_scoreboard_index_1.png",
            "templates/previews/template_report_scoreboard_index_2.png",
        ]
        for relative_path in expected_images:
            self.assertTrue((ROOT / relative_path).exists(), relative_path)
            self.assertIn(f"]({relative_path})", readme)

    def test_template_ppt_contains_score_board_examples(self) -> None:
        prs = Presentation(ROOT / "templates" / "template_report.pptx")
        self.assertGreaterEqual(len(prs.slides), 5)
        slide_text = "\n".join(
            shape.text
            for slide in prs.slides
            for shape in slide.shapes
            if hasattr(shape, "text")
        )
        self.assertIn("Score Board - Index 1", slide_text)
        self.assertIn("Score Board - Index 2", slide_text)


if __name__ == "__main__":
    unittest.main()
