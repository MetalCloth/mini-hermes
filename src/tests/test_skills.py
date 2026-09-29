import tempfile
import unittest
from pathlib import Path

from src.agent.skills import MAX_SKILL_BYTES, discover_skills, load_skill


class SkillLoadingTests(unittest.TestCase):
    def test_discovers_valid_frontmatter_and_rejects_malformed_or_oversized_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            skills = root / ".agents" / "skills"
            (skills / "code-review").mkdir(parents=True)
            (skills / "code-review" / "SKILL.md").write_text(
                "---\nname: code-review\ndescription: Review code changes.\n---\n"
                "Inspect correctness and preserve unrelated changes.\n",
                encoding="utf-8",
            )
            (skills / "missing").mkdir()
            (skills / "oversized").mkdir()
            (skills / "oversized" / "SKILL.md").write_bytes(b"x" * (MAX_SKILL_BYTES + 1))

            available, problems = discover_skills(root, root / "user-skills")

            self.assertEqual(set(available), {"code-review"})
            self.assertEqual(available["code-review"].instructions,
                             "Inspect correctness and preserve unrelated changes.")
            self.assertIn(("missing", "skill file is missing"), problems)
            self.assertTrue(any(name == "oversized" and "byte limit" in detail
                                for name, detail in problems))
            with self.assertRaisesRegex(ValueError, "unavailable: skill file is missing"):
                load_skill(root, "missing", root / "user-skills")

    def test_duplicate_skill_name_does_not_silently_override_project_skill(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / ".agents" / "skills" / "shared"
            global_skill = root / "user-skills" / "shared"
            project.mkdir(parents=True)
            global_skill.mkdir(parents=True)
            for path, text in (
                (project / "SKILL.md", "Project instructions."),
                (global_skill / "SKILL.md", "User instructions."),
            ):
                path.write_text(f"---\nname: shared\ndescription: Shared skill.\n---\n{text}\n")
            available, problems = discover_skills(root, root / "user-skills")
            self.assertEqual(available["shared"].instructions, "Project instructions.")
            self.assertTrue(any("duplicate skill name" in detail for _, detail in problems))


if __name__ == "__main__":
    unittest.main()
