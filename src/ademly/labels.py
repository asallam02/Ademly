"""Checks the ranking against your own judgment of good and bad fits."""

import csv
from dataclasses import dataclass, field
from pathlib import Path

from ademly.db import Database

FIELDS = ["job_id", "company", "title", "location", "url", "label"]
GOOD = {"good", "g", "yes", "y"}
BAD = {"bad", "b", "no", "n"}


def export_labels(db: Database, path: Path, count: int) -> int:
    """Write a mix of high- and low-scoring jobs for labeling, without scores."""
    ranked = db.ranked_jobs()
    if len(ranked) <= count:
        sample = ranked
    else:
        top = ranked[: count // 2]
        rest = ranked[count // 2 :]
        step = len(rest) / (count - len(top))
        sample = top + [rest[int(i * step)] for i in range(count - len(top))]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for job in sorted(sample, key=lambda row: row["id"]):
            writer.writerow({key: job[key] if key != "job_id" else job["id"] for key in FIELDS[:-1]})
    return len(sample)


@dataclass
class Agreement:
    labeled: int = 0
    agreed: int = 0
    disagreements: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.agreed / self.labeled if self.labeled else 0.0


def score_labels(db: Database, path: Path, min_score: int) -> Agreement:
    result = Agreement()
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            label = (row.get("label") or "").strip().lower()
            if label not in GOOD | BAD:
                continue
            job = db.job(int(row["job_id"]))
            predicted_good = job["score"] is not None and job["score"] >= min_score
            result.labeled += 1
            if predicted_good == (label in GOOD):
                result.agreed += 1
            else:
                result.disagreements.append(
                    f"job {job['id']} {job['company']} - {job['title']}: "
                    f"you said {label}, score {job['score']}"
                )
    return result
