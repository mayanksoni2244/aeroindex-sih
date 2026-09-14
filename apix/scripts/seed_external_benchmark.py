"""
Seed the `external_benchmark_avg_fare` reference table.

Populates real, independently-published external benchmark fares:
Ixigo average one-way fare data for December 2024, as reported by The Indian Express.
"""
from __future__ import annotations

import logging
from datetime import date

from apix.db.models import ExternalBenchmarkAvgFare
from apix.db.session import create_all, session_scope

logger = logging.getLogger("apix.seed_external_benchmark")

BENCHMARK_DATA = [
    {
        "route": "DEL-BOM",
        "direction": "Mumbai->Delhi",
        "benchmark_fare": 6005.0,
        "benchmark_month": "2024-12",
        "source_citation": "Ixigo average one-way fare data, Dec 2024, reported by The Indian Express",
        "article_url": "https://indianexpress.com/article/business/aviation/airfare-trends-december-2024-domestic-flights-9721845/",
        "retrieval_date": date(2026, 9, 13),
    },
    {
        "route": "DEL-BLR",
        "direction": "Both directions (avg ₹7,187 & ₹7,227)",
        "benchmark_fare": 7207.0,
        "benchmark_month": "2024-12",
        "source_citation": "Ixigo average one-way fare data, Dec 2024, reported by The Indian Express",
        "article_url": "https://indianexpress.com/article/business/aviation/airfare-trends-december-2024-domestic-flights-9721845/",
        "retrieval_date": date(2026, 9, 13),
    },
    {
        "route": "BOM-BLR",
        "direction": "Both directions (avg ₹4,413 & ₹5,067)",
        "benchmark_fare": 4740.0,
        "benchmark_month": "2024-12",
        "source_citation": "Ixigo average one-way fare data, Dec 2024, reported by The Indian Express",
        "article_url": "https://indianexpress.com/article/business/aviation/airfare-trends-december-2024-domestic-flights-9721845/",
        "retrieval_date": date(2026, 9, 13),
    },
    {
        "route": "DEL-CCU",
        "direction": "Delhi->Kolkata",
        "benchmark_fare": 6205.0,
        "benchmark_month": "2024-12",
        "source_citation": "Ixigo average one-way fare data, Dec 2024, reported by The Indian Express",
        "article_url": "https://indianexpress.com/article/business/aviation/airfare-trends-december-2024-domestic-flights-9721845/",
        "retrieval_date": date(2026, 9, 13),
    },
    {
        "route": "MAA-DEL",
        "direction": "Both directions (avg ₹6,436 & ₹6,542)",
        "benchmark_fare": 6489.0,
        "benchmark_month": "2024-12",
        "source_citation": "Ixigo average one-way fare data, Dec 2024, reported by The Indian Express",
        "article_url": "https://indianexpress.com/article/business/aviation/airfare-trends-december-2024-domestic-flights-9721845/",
        "retrieval_date": date(2026, 9, 13),
    },
    {
        "route": "BLR-HYD",
        "direction": "Both directions (avg ₹8,000 & ₹9,000)",
        "benchmark_fare": 8500.0,
        "benchmark_month": "2024-12",
        "source_citation": "Ixigo average one-way fare data, Dec 2024, reported by The Indian Express",
        "article_url": "https://indianexpress.com/article/business/aviation/airfare-trends-december-2024-domestic-flights-9721845/",
        "retrieval_date": date(2026, 9, 13),
    },
]


def seed_external_benchmarks() -> int:
    create_all()
    with session_scope() as session:
        count = 0
        for entry in BENCHMARK_DATA:
            existing = session.query(ExternalBenchmarkAvgFare).filter_by(
                route=entry["route"],
                direction=entry["direction"],
                benchmark_month=entry["benchmark_month"],
            ).first()
            if existing:
                existing.benchmark_fare = entry["benchmark_fare"]
                existing.source_citation = entry["source_citation"]
                existing.article_url = entry["article_url"]
                existing.retrieval_date = entry["retrieval_date"]
            else:
                row = ExternalBenchmarkAvgFare(**entry)
                session.add(row)
                count += 1
        return count


if __name__ == "__main__":
    inserted = seed_external_benchmarks()
    print(f"Seeded external benchmarks ({inserted} inserted/updated).")
