"""What an investigation process reads from files before it starts working.

``load_skill_catalog`` loads and validates the skills once; the API, the command line
and the evaluation call it before anything else and hand the catalog down, so every
investigation of one process sees the same skills. A skill that breaks a rule of the
format stops the process. In ``recorded`` mode the prompt files and the skills must also
be those the recordings were made with: a difference stops the process naming the file,
instead of surfacing later as a request without a recording.
"""

from __future__ import annotations

from bianque.agents.prompting import prompt_hashes
from bianque.config import Settings
from bianque.config.manifest import MANIFEST_FILE, check_files, load_manifest
from bianque.skills import SkillCatalog, load_skills


def load_skill_catalog(settings: Settings) -> SkillCatalog:
    """Load the skills of ``settings.skills_dir``; in ``recorded`` mode check the files.

    Raises:
        SkillError: The skills directory is missing or a skill breaks a rule.
        ManifestError: ``recorded`` mode, and a prompt file or a ``SKILL.md`` differs from
            the recording manifest.
    """
    catalog = load_skills(settings.skills_dir)
    if settings.llm_mode == "recorded":
        manifest = load_manifest(settings.llm_recordings_dir)
        if manifest is not None:
            check_files(
                manifest,
                settings.llm_recordings_dir / MANIFEST_FILE,
                prompts=prompt_hashes(),
                skills=catalog.hashes(),
                skills_dir=settings.skills_dir,
            )
    return catalog
