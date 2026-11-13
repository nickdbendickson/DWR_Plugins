"""Both personas carry their DOMAIN CONTEXT and HOW YOU ANSWER blocks, verbatim, in this repo's installers.

The blocks live in prompts/*.txt, one pair per AI, and are embedded in setup-local-heim.sh and
setup-vor.zsh. A block edited in one place and not the other is invisible until someone asks the
model a question, so this holds them together. damit carries the fuller version of this test (it
has three installers per AI); this is the same contract for the two this repo ships.

The character line each AI has is in its banner only. It greets and never answers, so it cannot
change what the model asserts - which is the reason it must never reach a SYSTEM block.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
HEIM = os.path.abspath(os.path.join(HERE, ".."))
VOR = os.path.abspath(os.path.join(HEIM, "..", "vor"))

HEIM_SH = os.path.join(HEIM, "setup-local-heim.sh")
VOR_SH = os.path.join(VOR, "setup-vor.zsh")
BLOCKS = {
    "heim": (os.path.join(HEIM, "prompts", "domain_context.txt"), os.path.join(HEIM, "prompts", "manner.txt")),
    "vor": (os.path.join(VOR, "prompts", "domain_context.txt"), os.path.join(VOR, "prompts", "manner.txt")),
}
VOICE = {
    "heim": "It watches and explains; the code sets the verdict.",
    "vor": "Every sentence traces to a page, or it says it cannot.",
}


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def block(path):
    return slurp(path).rstrip("\n")


def persona_system(path, lead):
    t = slurp(path)
    i = t.index(lead)
    return t[i:t.index('"""', i + len(lead))]


def systems():
    return {"heim": persona_system(HEIM_SH, 'SYSTEM """You are Heim-Lite'),
            "vor": persona_system(VOR_SH, 'SYSTEM """You are Vor,')}


class PersonaBlocksTest(unittest.TestCase):
    def test_each_installer_carries_both_of_its_blocks(self):
        for ai, system in systems().items():
            for path in BLOCKS[ai]:
                self.assertIn(block(path), system, "%s: %s" % (ai, os.path.basename(path)))

    def test_no_character_that_an_unquoted_heredoc_would_eat(self):
        """Both Modelfiles are written with an unquoted heredoc: a $, a backtick or a backslash is
        expanded before Ollama sees it, and a triple quote ends the SYSTEM block early."""
        for paths in BLOCKS.values():
            for path in paths:
                self.assertNotRegex(block(path), r'[$`\\]', path)
                self.assertNotIn('"""', block(path), path)

    def test_both_say_there_is_no_web_access_and_that_material_stays_local(self):
        for ai in BLOCKS:
            context = block(BLOCKS[ai][0])
            self.assertIn("NO web access", context, ai)
            self.assertRegex(context, r"(?i)stays? on this machine", ai)

    def test_the_manner_is_behaviour_not_adjectives(self):
        for ai in BLOCKS:
            lines = [ln[2:] for ln in block(BLOCKS[ai][1]).splitlines() if ln.startswith("- ")]
            self.assertGreaterEqual(len(lines), 5, ai)
            for ln in lines:
                self.assertNotRegex(ln, r"(?i)^(you are|be )\b", "%s: %s" % (ai, ln))
        for paths in BLOCKS.values():
            for path in paths:
                self.assertNotRegex(block(path),
                                    r"(?i)\b(personality|curious|thoughtful|engaging|friendly|witty|tone|voice)\b", path)

    def test_the_character_line_is_in_the_banner_and_in_no_persona(self):
        self.assertIn(VOICE["heim"], slurp(HEIM_SH))
        self.assertIn(VOICE["vor"], slurp(os.path.join(VOR, "vor.py")))
        for ai, system in systems().items():
            for line in VOICE.values():
                self.assertNotIn(line, system, ai)

    def test_heim_no_longer_names_the_archived_plugins(self):
        context = block(BLOCKS["heim"][0])
        for gone in ("ND_Dam_Hazard_Helper", "nd_breach_runner", "nd_dam_screening", "nd_data_updater"):
            self.assertNotIn(gone, context, gone)

    def test_every_publication_vor_names_is_in_the_source_catalog(self):
        flat = re.sub(r"[^a-z0-9]+", "", slurp(os.path.join(VOR, "reference_sources.json")).lower())
        for name in ("ACER TM-11", "FEMA 333", "FEMA P-946", "FEMA P-919", "TR-60", "TR-66",
                     "EM 1110-2-2300", "ER 1110-2-1156", "HMR 51", "Design of Small Dams"):
            self.assertIn(re.sub(r"[^a-z0-9]+", "", name.lower()), flat, name)


if __name__ == "__main__":
    unittest.main()
