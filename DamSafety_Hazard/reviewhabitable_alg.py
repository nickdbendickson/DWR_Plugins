# -*- coding: utf-8 -*-
"""ndhazard:reviewhabitable - the engineer's habitability review of structures (2.24.7).

Only an ND 911 address point is habitable by itself. Every other structure (NSI, building
outlines, ...) is "unverified" until an engineer reviews it here: this tool saves
reviewed_habitable = yes / no (or removes the review) for the structure keys it is given,
in habitable_review.json in the dam folder. Classify and the depth-velocity chart only READ
that file; this tool is the only thing that writes it, and only when run. The keys are the
struct_key column of the chart's worklist (ND_DV_Habitable_unverified.csv). Aligned with
damit:reviewhabitable (damit PR #66).
"""
import os

from qgis.core import (
    QgsProcessingException, QgsProcessingParameterFile, QgsProcessingParameterEnum,
    QgsProcessingParameterString, QgsProcessingOutputFile,
)

from .processing_provider import _Base, FILE_FOLDER

DECISIONS = [
    "Habitable (an engineer reviewed: people live or work there)",
    "Not habitable (reviewed)",
    "Remove the review (back to unverified)",
]
# yes / no / None (remove) - DamSafety Hazard's words
VALUES = ["yes", "no", None]


class ReviewHabitableAlgorithm(_Base):
    def name(self):
        return "reviewhabitable"

    def displayName(self):
        return "Review habitable structures (engineer sign-off)"

    def shortHelpString(self):
        return ("Saves an engineer's habitability decision for structures that are not ND 911 "
                "address points. Only ND 911 address points are habitable by themselves; every "
                "other structure stays 'unverified' (counted, not plotted on the depth-velocity "
                "chart) until it is reviewed here. The decision goes into habitable_review.json "
                "in the dam folder; classify and the chart only read it. This tool is the only "
                "writer, and only when you run it (a classify run never creates or changes the "
                "file). Give the structure keys (the struct_key column of "
                "ND_DV_Habitable_unverified.csv, typed or as the CSV), the decision and who "
                "decided. 'Remove' deletes the review. Every change is kept in the file's "
                "history. The existing Review tab (Structure_Review_Decisions.csv) still applies "
                "category / type / persons decisions as before.")

    def initAlgorithm(self, config=None):
        P = self.addParameter
        P(QgsProcessingParameterFile(
            "FOLDER", "Dam folder (habitable_review.json is created or updated in it) "
            "or the path of the review file", behavior=FILE_FOLDER))
        P(QgsProcessingParameterString(
            "KEYS", "Structure keys (comma, semicolon or one per line; the struct_key "
            "column of the worklist)", multiLine=True, optional=True))
        P(QgsProcessingParameterFile(
            "KEYS_CSV", "Or a CSV with a struct_key column (e.g. the unverified worklist, "
            "edited to the rows reviewed)", optional=True, extension="csv"))
        P(QgsProcessingParameterEnum("DECISION", "Decision", options=list(DECISIONS),
                                     defaultValue=0))
        P(QgsProcessingParameterString("REVIEWER", "Reviewed by (name of the engineer)"))
        P(QgsProcessingParameterString("NOTE", "Note (why; e.g. site visit, aerial photo)",
                                       optional=True))
        self.addOutput(QgsProcessingOutputFile("REVIEW_FILE", "habitable_review.json"))

    def processAlgorithm(self, parameters, context, feedback):
        from . import habitable_review as HR
        folder = (self.parameterAsFile(parameters, "FOLDER", context)
                  or self.parameterAsString(parameters, "FOLDER", context) or "").strip()
        if not folder:
            raise QgsProcessingException("Give the dam folder or the review file path.")
        path = folder if folder.lower().endswith(".json") else os.path.join(
            folder, HR.REVIEW_FILE)
        try:
            keys = HR.keys_from(self.parameterAsString(parameters, "KEYS", context),
                                self.parameterAsFile(parameters, "KEYS_CSV", context) or None)
            dec = VALUES[int(self.parameterAsEnum(parameters, "DECISION", context))]
            who = self.parameterAsString(parameters, "REVIEWER", context)
            HR.sign_off(path, keys, dec, who,
                        self.parameterAsString(parameters, "NOTE", context))
        except (ValueError, OSError, IndexError) as e:
            raise QgsProcessingException(str(e))
        label = {HR.YES: "reviewed habitable (yes)", HR.NO: "reviewed not habitable (no)",
                 None: "review removed (unverified)"}[dec]
        feedback.pushInfo("{} structure(s): {} in {}".format(len(keys), label, path))
        return {"REVIEW_FILE": path}
