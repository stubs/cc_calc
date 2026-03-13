#!/usr/bin/env python3
"""
Transaction Tag Matcher Script

Matches Chase credit card transactions with Copilot transactions to apply tags,
then output a rich summary table by tag.
"""

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import questionary
from rapidfuzz import fuzz
from rich.console import Console
from rich.table import Table


@dataclass
class ChaseTransaction:
    """A Chase credit card transaction."""
    transaction_date: datetime
    post_date: datetime
    description: str
    category: str
    amount: float  # INFO: stored as abs for comparison
    original_amount: float  # INFO: negative for purchases
    tag: Optional[str] = None


@dataclass
class CopilotTransaction:
    """A Copilot transaction."""
    date: datetime
    name: str
    amount: float  # Positive for purchases
    tags: str
    excluded: bool


def parse_date(date_str: str, formats: Optional[list[str]] = None) -> datetime:
    """Parse a date string trying multiple formats."""
    if formats is None:
        formats = ["%m/%d/%Y", "%Y-%m-%d"]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Could not parse date: {date_str}")


def load_chase_csv(filepath: str) -> list[ChaseTransaction]:
    """Load and parse Chase CSV file."""
    transactions = []

    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            original_amount = float(row['Amount'])

            # INFO: skip payments/credits
            if original_amount < 0:
                transactions.append(ChaseTransaction(
                    transaction_date=parse_date(row['Transaction Date']),
                    post_date=parse_date(row['Post Date']),
                    description=row['Description'],
                    category=row.get('Category', ''),
                    amount=abs(original_amount),
                    original_amount=original_amount,
                ))

    return transactions


def load_copilot_csv(filepath: str) -> list[CopilotTransaction]:
    """Load and parse Copilot CSV file."""
    transactions = []

    with open(filepath, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            amount = float(row['amount'])

            # INFO: skip negative amounts (credits/payments in Copilot)
            if amount > 0:
                transactions.append(CopilotTransaction(
                    date=parse_date(row['date']),
                    name=row['name'],
                    amount=amount,
                    tags=row.get('tags', ''),
                    excluded=row.get('excluded', 'false').lower() == 'true',
                ))

    return transactions


def normalize_name(name: str) -> str:
    """Normalize a merchant name for comparison."""
    # Remove common prefixes and clean up
    prefixes = ['TST*', 'SQ *', 'SP ', 'SEAMLSS*', 'SPO*', 'FSP*', 'FSI*', 'SPI*',
                'MTA*', 'IN *', 'ANC*', 'PY *', 'T2*']

    name = name.upper().strip()
    for prefix in prefixes:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break

    # Remove trailing location codes and numbers
    parts = name.split()
    if parts and parts[-1].isdigit():
        parts = parts[:-1]

    return ' '.join(parts)


def fuzzy_match_name(chase_desc: str, copilot_name: str, threshold: int = 70) -> bool:
    """Check if merchant names match using fuzzy matching."""
    chase_normalized = normalize_name(chase_desc)
    copilot_normalized = copilot_name.upper().strip()

    # Try partial ratio for substring matching (handles cases like "LINCOLN MARKET-210" vs "Lincoln")
    score = fuzz.partial_ratio(chase_normalized, copilot_normalized)
    if score >= threshold:
        return True

    # Try token set ratio for word matching
    score = fuzz.token_set_ratio(chase_normalized, copilot_normalized)
    return score >= threshold


def amounts_match(chase_amount: float, copilot_amount: float, tolerance: float = 0.01) -> bool:
    """Check if amounts match within tolerance."""
    return abs(chase_amount - copilot_amount) <= tolerance


def find_match(
    chase_tx: ChaseTransaction,
    copilot_txs: list[CopilotTransaction],
    used_indices: set[int]
) -> Optional[int]:
    """
    Find a matching Copilot transaction for a Chase transaction.

    Matching strategy:
    1. Primary: Transaction Date + Amount + Fuzzy Name
    2. Fallback 1: Post Date + Amount + Fuzzy Name
    3. Fallback 2: Date +/- 1 day + Amount (no name check)

    Returns the index of the matching Copilot transaction, or None.
    """
    # Strategy 1: Transaction Date + Amount + Fuzzy Name
    for idx, copilot_tx in enumerate(copilot_txs):
        if idx in used_indices:
            continue
        if (chase_tx.transaction_date == copilot_tx.date and
            amounts_match(chase_tx.amount, copilot_tx.amount) and
            fuzzy_match_name(chase_tx.description, copilot_tx.name)):
            return idx

    # Strategy 2: Post Date + Amount + Fuzzy Name
    for idx, copilot_tx in enumerate(copilot_txs):
        if idx in used_indices:
            continue
        if (chase_tx.post_date == copilot_tx.date and
            amounts_match(chase_tx.amount, copilot_tx.amount) and
            fuzzy_match_name(chase_tx.description, copilot_tx.name)):
            return idx

    # Strategy 3: Date +/- 3 days + Amount (relaxed matching)
    for idx, copilot_tx in enumerate(copilot_txs):
        if idx in used_indices:
            continue
        date_diff = abs((chase_tx.transaction_date - copilot_tx.date).days)
        if date_diff <= 3 and amounts_match(chase_tx.amount, copilot_tx.amount):
            return idx

    # Strategy 4: Post Date +/- 3 days + Amount
    for idx, copilot_tx in enumerate(copilot_txs):
        if idx in used_indices:
            continue
        date_diff = abs((chase_tx.post_date - copilot_tx.date).days)
        if date_diff <= 3 and amounts_match(chase_tx.amount, copilot_tx.amount):
            return idx

    return None


def match_transactions(
    chase_txs: list[ChaseTransaction],
    copilot_txs: list[CopilotTransaction]
) -> tuple[list[ChaseTransaction], list[ChaseTransaction]]:
    """
    Match Chase transactions with Copilot transactions and apply tags.

    Returns:
        Tuple of (matched_transactions, unmatched_transactions)
    """
    matched = []
    unmatched = []
    used_copilot_indices: set[int] = set()

    for chase_tx in chase_txs:
        match_idx = find_match(chase_tx, copilot_txs, used_copilot_indices)

        if match_idx is not None:
            used_copilot_indices.add(match_idx)
            copilot_tx = copilot_txs[match_idx]
            chase_tx.tag = copilot_tx.tags if copilot_tx.tags else None
            matched.append(chase_tx)
        else:
            unmatched.append(chase_tx)

    return matched, unmatched


def aggregate_by_tag(transactions: list[ChaseTransaction]) -> dict[str, float]:
    """Aggregate transaction amounts by tag."""
    totals: dict[str, float] = defaultdict(float)

    for tx in transactions:
        tag = tx.tag if tx.tag else "(no tag)"
        totals[tag] += tx.amount

    return dict(sorted(totals.items(), key=lambda x: x[1], reverse=True))


def find_potential_matches(
    chase_tx: ChaseTransaction,
    copilot_txs: list[CopilotTransaction],
    used_indices: set[int],
    max_results: int = 10
) -> list[tuple[int, CopilotTransaction, str]]:
    """
    Find potential matches for an unmatched Chase transaction.

    Returns list of (index, copilot_tx, match_reason) tuples, scored by relevance.
    """
    candidates = []

    for idx, copilot_tx in enumerate(copilot_txs):
        if idx in used_indices:
            continue

        score = 0
        reasons = []

        # Check amount match
        if amounts_match(chase_tx.amount, copilot_tx.amount):
            score += 50
            reasons.append("exact amount")
        elif abs(chase_tx.amount - copilot_tx.amount) <= 1.0:
            score += 20
            reasons.append("~amount")
        else:
            # Skip if amount is way off
            continue

        # Check date proximity
        tx_date_diff = abs((chase_tx.transaction_date - copilot_tx.date).days)
        post_date_diff = abs((chase_tx.post_date - copilot_tx.date).days)
        min_date_diff = min(tx_date_diff, post_date_diff)

        if min_date_diff == 0:
            score += 30
            reasons.append("same date")
        elif min_date_diff <= 1:
            score += 20
            reasons.append("±1 day")
        elif min_date_diff <= 3:
            score += 10
            reasons.append(f"±{min_date_diff} days")
        elif min_date_diff <= 7:
            score += 5
            reasons.append(f"±{min_date_diff} days")
        else:
            # Skip if date is too far off
            continue

        # Check name similarity
        name_score = fuzz.token_set_ratio(
            normalize_name(chase_tx.description),
            copilot_tx.name.upper()
        )
        if name_score >= 70:
            score += 20
            reasons.append(f"name~{name_score}%")
        elif name_score >= 50:
            score += 10
            reasons.append(f"name~{name_score}%")

        if score > 0:
            candidates.append((idx, copilot_tx, score, ", ".join(reasons)))

    # Sort by score descending
    candidates.sort(key=lambda x: x[2], reverse=True)

    # Return top results without the score
    return [(idx, tx, reason) for idx, tx, score, reason in candidates[:max_results]]


def interactive_match_unmatched(
    unmatched: list[ChaseTransaction],
    copilot_txs: list[CopilotTransaction],
    used_indices: set[int],
    console: Console
) -> list[ChaseTransaction]:
    """
    Interactively match unmatched transactions using a picker.

    Returns list of newly matched transactions.
    """
    newly_matched = []
    remaining_unmatched = []

    console.print()
    console.print("[bold yellow]Interactive Matching Mode[/bold yellow]")
    console.print("For each unmatched transaction, select a match or skip.\n")

    for chase_tx in unmatched:
        # Find potential matches
        potentials = find_potential_matches(chase_tx, copilot_txs, used_indices)

        if not potentials:
            console.print(f"[dim]No potential matches for: {chase_tx.description} (${chase_tx.amount:.2f})[/dim]")
            remaining_unmatched.append(chase_tx)
            continue

        # Build choices for the picker
        console.print(f"\n[bold]Chase:[/bold] {chase_tx.transaction_date.strftime('%Y-%m-%d')} | "
                      f"[cyan]{chase_tx.description}[/cyan] | ${chase_tx.amount:.2f}")

        choices = []
        for idx, copilot_tx, reason in potentials:
            tag_display = f" [{copilot_tx.tags}]" if copilot_tx.tags else ""
            choice_text = (
                f"{copilot_tx.date.strftime('%Y-%m-%d')} | "
                f"{copilot_tx.name} | "
                f"${copilot_tx.amount:.2f}{tag_display} "
                f"({reason})"
            )
            choices.append(questionary.Choice(title=choice_text, value=idx))

        choices.append(questionary.Choice(title="[Skip - no match]", value=None))
        choices.append(questionary.Choice(title="[Quit interactive mode]", value="quit"))

        answer = questionary.select(
            "Select matching Copilot transaction:",
            choices=choices,
        ).ask()

        if answer == "quit":
            # Add remaining unmatched and break
            remaining_unmatched.append(chase_tx)
            remaining_unmatched.extend(unmatched[unmatched.index(chase_tx) + 1:])
            break
        elif answer is None:
            remaining_unmatched.append(chase_tx)
        else:
            # Match found
            used_indices.add(answer)
            copilot_tx = copilot_txs[answer]
            chase_tx.tag = copilot_tx.tags if copilot_tx.tags else None
            newly_matched.append(chase_tx)
            console.print(f"[green]✓ Matched with tag: {chase_tx.tag or '(no tag)'}[/green]")

    return newly_matched, remaining_unmatched


def display_results(
    tag_totals: dict[str, float],
    unmatched: list[ChaseTransaction],
    matched_count: int,
    console: Console,
    matched_transactions: list[ChaseTransaction] = None,
    show_details: bool = False
) -> None:
    """Display results using rich tables."""
    # Summary stats
    total_matched = sum(tag_totals.values())
    total_unmatched = sum(tx.amount for tx in unmatched)

    console.print()
    console.print(f"[bold]Matched:[/bold] {matched_count} transactions (${total_matched:,.2f})")
    console.print(f"[bold]Unmatched:[/bold] {len(unmatched)} transactions (${total_unmatched:,.2f})")
    console.print()

    # Tag summary table
    tag_table = Table(title="Spending by Tag", show_footer=True)
    tag_table.add_column("Tag", style="cyan", footer="[bold]Total[/bold]")
    tag_table.add_column("Amount", justify="right", style="green",
                         footer=f"[bold]${total_matched:,.2f}[/bold]")

    for tag, amount in tag_totals.items():
        tag_table.add_row(tag, f"${amount:,.2f}")

    console.print(tag_table)

    # Detailed transactions by tag
    if show_details and matched_transactions:
        # Group transactions by tag
        by_tag: dict[str, list[ChaseTransaction]] = defaultdict(list)
        for tx in matched_transactions:
            tag = tx.tag if tx.tag else "(no tag)"
            by_tag[tag].append(tx)

        for tag in tag_totals.keys():
            console.print()
            tag_txs = by_tag.get(tag, [])
            tag_total = sum(tx.amount for tx in tag_txs)

            detail_table = Table(title=f"[cyan]{tag}[/cyan] - {len(tag_txs)} transactions", show_footer=True)
            detail_table.add_column("Date", style="dim", footer="[bold]Total[/bold]")
            detail_table.add_column("Description")
            detail_table.add_column("Category", style="dim")
            detail_table.add_column("Amount", justify="right", style="green",
                                    footer=f"[bold]${tag_total:,.2f}[/bold]")

            for tx in sorted(tag_txs, key=lambda x: x.transaction_date, reverse=True):
                detail_table.add_row(
                    tx.transaction_date.strftime("%Y-%m-%d"),
                    tx.description[:40],
                    tx.category,
                    f"${tx.amount:,.2f}"
                )

            console.print(detail_table)

    # Unmatched transactions table
    if unmatched:
        console.print()
        unmatched_table = Table(title="Unmatched Chase Transactions", show_footer=True)
        unmatched_table.add_column("Date", style="dim", footer="[bold]Total[/bold]")
        unmatched_table.add_column("Description")
        unmatched_table.add_column("Category", style="dim")
        unmatched_table.add_column("Amount", justify="right", style="red",
                                   footer=f"[bold]${total_unmatched:,.2f}[/bold]")

        # Sort by date descending
        for tx in sorted(unmatched, key=lambda x: x.transaction_date, reverse=True):
            unmatched_table.add_row(
                tx.transaction_date.strftime("%Y-%m-%d"),
                tx.description[:40],
                tx.category,
                f"${tx.amount:,.2f}"
            )

        console.print(unmatched_table)


def main():
    parser = argparse.ArgumentParser(
        description="Match Chase transactions with Copilot data and summarize by tag."
    )
    parser.add_argument(
        "CHASE_CSV",
        nargs="+",
        help="Path(s) to one or more Chase CSV export files"
    )
    parser.add_argument(
        "COPILOT_CSV",
        help="Path to Copilot CSV export file"
    )
    parser.add_argument(
        "--fuzzy-threshold",
        type=int,
        default=70,
        help="Fuzzy matching threshold (0-100, default: 70)"
    )
    parser.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="Interactively match unmatched transactions"
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="List individual transactions grouped by tag"
    )

    args = parser.parse_args()
    console = Console()

    # Load transactions
    chase_txs = []
    for chase_file in args.CHASE_CSV:
        console.print(f"[dim]Loading Chase transactions from {chase_file}...[/dim]")
        chase_txs.extend(load_chase_csv(chase_file))
    console.print(f"[dim]Loaded {len(chase_txs)} Chase transactions total[/dim]")

    console.print("[dim]Loading Copilot transactions...[/dim]")
    copilot_txs = load_copilot_csv(args.COPILOT_CSV)
    console.print(f"[dim]Loaded {len(copilot_txs)} Copilot transactions[/dim]")

    # Match transactions
    console.print("[dim]Matching transactions...[/dim]")
    matched, unmatched = match_transactions(chase_txs, copilot_txs)

    # Track used Copilot indices for interactive matching
    used_indices: set[int] = set()
    for chase_tx in matched:
        # Find which Copilot transaction was used
        for idx, copilot_tx in enumerate(copilot_txs):
            if idx in used_indices:
                continue
            if (amounts_match(chase_tx.amount, copilot_tx.amount) and
                chase_tx.tag == (copilot_tx.tags if copilot_tx.tags else None)):
                used_indices.add(idx)
                break

    # Interactive matching for unmatched transactions
    if args.interactive and unmatched:
        newly_matched, unmatched = interactive_match_unmatched(
            unmatched, copilot_txs, used_indices, console
        )
        matched.extend(newly_matched)

    # Aggregate by tag
    tag_totals = aggregate_by_tag(matched)

    # Display results
    display_results(tag_totals, unmatched, len(matched), console,
                    matched_transactions=matched, show_details=args.list)


if __name__ == "__main__":
    main()
