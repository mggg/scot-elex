"""Find and conservatively repair truncated candidate names using BLT sources."""

from __future__ import annotations

import argparse
import csv
import io
import re
import shlex
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CandidateFix:
    csv_path: Path
    candidate_number: int
    old_name: str
    new_name: str
    reason: str


@dataclass(frozen=True)
class CandidateWarning:
    csv_path: Path
    candidate_number: int | None
    message: str


def repair_truncated_candidate_names(
    root: str | Path = ".",
    apply: bool = False,
) -> tuple[list[CandidateFix], list[CandidateWarning]]:
    """Compare candidate-count CSVs with BLTs and optionally apply safe fixes."""
    root = Path(root)
    blt_root = root / "blt_files"
    if not blt_root.is_dir():
        raise ValueError(f"Missing BLT directory: {blt_root}")

    csv_paths = sorted(root.glob("*_cands/*.csv"))
    if not csv_paths:
        raise ValueError(f"No candidate-count CSV files found under {root}")

    fixes: list[CandidateFix] = []
    warnings: list[CandidateWarning] = []

    for csv_path in csv_paths:
        blt_path = blt_root / csv_path.parent.name / f"{csv_path.stem}.blt"
        if not blt_path.exists():
            warnings.append(
                CandidateWarning(csv_path, None, f"Missing BLT file: {blt_path}")
            )
            continue

        try:
            csv_candidates = _candidate_rows_from_csv(csv_path)
            blt_candidates = _candidate_names_from_blt(blt_path)
        except ValueError as exc:
            warnings.append(CandidateWarning(csv_path, None, str(exc)))
            continue

        if len(csv_candidates) != len(blt_candidates):
            warnings.append(
                CandidateWarning(
                    csv_path,
                    None,
                    "Candidate count mismatch: "
                    f"CSV has {len(csv_candidates)}, BLT has {len(blt_candidates)}",
                )
            )
            continue

        for candidate_number, ((line_index, old_name), blt_name) in enumerate(
            zip(csv_candidates, blt_candidates),
            start=1,
        ):
            new_name = _csv_style_name(blt_name)
            reason = _safe_fix_reason(old_name, new_name)

            if reason is None:
                if _normalized_name(old_name) != _normalized_name(new_name):
                    warnings.append(
                        CandidateWarning(
                            csv_path,
                            candidate_number,
                            f"Name mismatch not auto-fixed: "
                            f"{old_name!r} vs BLT {new_name!r}",
                        )
                    )
                continue

            if old_name == new_name:
                continue

            fix = CandidateFix(
                csv_path,
                candidate_number,
                old_name,
                new_name,
                reason,
            )
            fixes.append(fix)

            if apply:
                _replace_candidate_name(csv_path, line_index, old_name, new_name)
                _repair_seat_count_mirrors(
                    root,
                    csv_path,
                    candidate_number,
                    old_name,
                    new_name,
                    warnings,
                )

    return fixes, warnings


def _candidate_rows_from_csv(csv_path: Path) -> list[tuple[int, str]]:
    lines = csv_path.read_text(encoding="utf-8-sig").splitlines()
    candidates: list[tuple[int, str]] = []

    for line_index, line in enumerate(lines):
        row = next(csv.reader([line]))
        if not row or not row[0].startswith("Candidate "):
            continue

        expected = f"Candidate {len(candidates) + 1}"
        if row[0] != expected:
            raise ValueError(f"{csv_path}: expected {expected}, got {row[0]}")
        if len(row) < 2 or not row[1]:
            raise ValueError(f"{csv_path}: malformed {expected} row")
        candidates.append((line_index, row[1]))

    return candidates


def _candidate_names_from_blt(blt_path: Path) -> list[str]:
    lines = blt_path.read_text(encoding="utf-8-sig").splitlines()
    if not lines:
        raise ValueError(f"{blt_path}: empty BLT file")

    try:
        candidate_count = int(lines[0].split()[0])
    except (IndexError, ValueError) as exc:
        raise ValueError(f"{blt_path}: invalid BLT header") from exc

    ballot_end = next(
        (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "0"),
        None,
    )
    if ballot_end is None:
        raise ValueError(f"{blt_path}: missing ballot terminator")

    candidate_lines = lines[ballot_end + 1 : ballot_end + 1 + candidate_count]
    if len(candidate_lines) != candidate_count:
        raise ValueError(
            f"{blt_path}: expected {candidate_count} candidate lines, "
            f"found {len(candidate_lines)}"
        )

    names = [_candidate_name_from_blt_line(line) for line in candidate_lines]
    if any(not name for name in names):
        raise ValueError(f"{blt_path}: parsed an empty candidate name")
    return names


def _candidate_name_from_blt_line(line: str) -> str:
    line = line.strip()

    alternative = re.match(
        r"^#\s*ALTERNATIVE NAME\s+\d+:\s*(?P<rest>.*)$",
        line,
        re.IGNORECASE,
    )
    if alternative:
        line = alternative.group("rest").strip()

    if line.startswith('"'):
        fields = next(csv.reader([line]))
        if fields:
            field = fields[0].strip()
            quoted_chunks = re.findall(r'"([^"]+)"', field)
            before_first_quote = field.split('"', 1)[0].strip()

            if quoted_chunks:
                if before_first_quote and not _looks_like_party_label(
                    before_first_quote
                ):
                    return before_first_quote
                return quoted_chunks[0].strip()

            try:
                shell_parts = shlex.split(line)
            except ValueError:
                shell_parts = []
            if len(shell_parts) >= 2:
                return shell_parts[0].strip()
            return field.strip('" ')

    if line.count("(") != line.count(")"):
        return line

    previous = None
    while previous != line:
        previous = line
        line = re.sub(r"\s*\([^()]*\)\s*$", "", line).strip()
    return line


def _safe_fix_reason(old_name: str, new_name: str) -> str | None:
    old_normalized = _normalized_name(old_name)
    new_normalized = _normalized_name(new_name)

    if old_name == new_name:
        return None
    if "\ufffd" in new_name or _looks_mojibaked(new_name):
        return None
    if new_name.count("(") != new_name.count(")"):
        return None
    if (
        old_normalized == new_normalized
        and not _has_non_ascii(old_name)
        and _has_non_ascii(new_name)
    ):
        return "accent-restoration"
    if new_normalized.startswith(old_normalized) and len(new_normalized) > len(
        old_normalized
    ):
        return "truncated-prefix"
    if len(new_normalized) > len(old_normalized) and _token_prefix_match(
        old_name, new_name
    ):
        return "truncated-token-prefix"
    return None


def _replace_candidate_name(
    csv_path: Path,
    line_index: int,
    expected_old_name: str,
    new_name: str,
) -> None:
    """Replace one candidate name while preserving every other physical line."""
    text = csv_path.read_text(encoding="utf-8-sig")
    lines = text.splitlines(keepends=True)
    if line_index >= len(lines):
        raise ValueError(f"{csv_path}: candidate line index is out of range")

    physical_line = lines[line_index]
    row = next(csv.reader([physical_line.rstrip("\r\n")]))
    if len(row) < 2 or row[1] != expected_old_name:
        raise ValueError(
            f"{csv_path}: expected candidate name {expected_old_name!r} "
            f"on line {line_index + 1}"
        )
    row[1] = new_name

    newline = "\r\n" if physical_line.endswith("\r\n") else "\n"
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator=newline).writerow(row)
    lines[line_index] = output.getvalue()
    csv_path.write_text("".join(lines), encoding="utf-8", newline="")


def _repair_seat_count_mirrors(
    root: Path,
    source_csv: Path,
    candidate_number: int,
    old_name: str,
    new_name: str,
    warnings: list[CandidateWarning],
) -> None:
    mirrors = sorted(root.glob(f"*_seats/{source_csv.name}"))
    for mirror in mirrors:
        try:
            candidate_rows = _candidate_rows_from_csv(mirror)
            line_index, mirror_name = candidate_rows[candidate_number - 1]
            if mirror_name != old_name:
                raise ValueError(
                    f"expected {old_name!r}, found {mirror_name!r}"
                )
            _replace_candidate_name(mirror, line_index, old_name, new_name)
        except (IndexError, ValueError) as exc:
            warnings.append(
                CandidateWarning(
                    mirror,
                    candidate_number,
                    f"Seat-count mirror not updated: {exc}",
                )
            )


def _token_prefix_match(old_name: str, new_name: str) -> bool:
    old_tokens = [_normalized_name(token) for token in old_name.split()]
    new_tokens = [_normalized_name(token) for token in new_name.split()]
    if not old_tokens or len(old_tokens) > len(new_tokens):
        return False
    return all(
        new_token.startswith(old_token)
        for old_token, new_token in zip(old_tokens, new_tokens)
    )


def _normalized_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = decomposed.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", ascii_name.lower())


def _has_non_ascii(value: str) -> bool:
    return any(ord(character) > 127 for character in value)


def _looks_mojibaked(value: str) -> bool:
    return any(marker in value for marker in ["Ã", "ã", "Â", "â€"])


def _csv_style_name(blt_name: str) -> str:
    return " ".join(blt_name.split()).title()


def _looks_like_party_label(value: str) -> bool:
    known = {
        "con",
        "grn",
        "ind",
        "lab",
        "ld",
        "lib",
        "nf",
        "snp",
        "soc",
        "sol",
        "sscp",
        "ssp",
        "ukip",
    }
    tokens = value.split()
    if len(tokens) != 1:
        return False
    token = tokens[0]
    return token.lower().strip(".") in known or token == token.upper()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compare candidate names in *_cands CSVs with their BLT sources. "
            "Only conservative accent and truncation repairs are proposed."
        )
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="scot-elex repository root (default: directory containing this script)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply safe repairs; the default is a dry run",
    )
    args = parser.parse_args()

    fixes, warnings = repair_truncated_candidate_names(args.root, apply=args.apply)

    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"{mode}: {len(fixes)} fixable candidate names")
    for fix in fixes:
        print(
            f"{fix.csv_path}: Candidate {fix.candidate_number}: "
            f"{fix.old_name!r} -> {fix.new_name!r} ({fix.reason})"
        )

    print(f"Warnings: {len(warnings)}")
    for warning in warnings:
        candidate = (
            ""
            if warning.candidate_number is None
            else f" Candidate {warning.candidate_number}:"
        )
        print(f"{warning.csv_path}:{candidate} {warning.message}")


if __name__ == "__main__":
    main()
