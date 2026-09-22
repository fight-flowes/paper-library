from __future__ import annotations

from dataclasses import dataclass

from paper_library.metadata.service import GatewayResponse, enrich_bibliography


@dataclass
class FakeGateway:
    ready_response: GatewayResponse
    search_response: GatewayResponse
    extract_response: GatewayResponse

    def ready(self) -> GatewayResponse:
        return self.ready_response

    def search(self, _query: str) -> GatewayResponse:
        return self.search_response

    def extract(self, _urls: list[str]) -> GatewayResponse:
        return self.extract_response


def response(
    status_code: int,
    payload: dict[str, object],
    request_id: str,
) -> GatewayResponse:
    return GatewayResponse(status_code, payload, request_id)


def test_exact_official_result_is_verified() -> None:
    gateway = FakeGateway(
        response(200, {"status": "ready"}, "ready-1"),
        response(
            200,
            {
                "results": [
                    {
                        "title": "A Paper Title",
                        "url": "https://aclanthology.org/2025.acl-long.1/",
                        "content": "A Paper Title, ACL 2025.",
                    }
                ]
            },
            "search-1",
        ),
        response(
            200,
            {
                "results": [
                    {
                        "url": "https://aclanthology.org/2025.acl-long.1/",
                        "raw_content": """# A Paper Title
Authors: Jane Doe, John Roe
Venue: ACL 2025
Publication date: 2025-07-27
DOI: 10.1000/example
""",
                    }
                ],
                "failed_results": [],
            },
            "extract-1",
        ),
    )

    result = enrich_bibliography("A Paper Title", "2025", "ACL", gateway)

    assert result.status == "verified"
    assert result.confidence == 1.0
    assert result.bibliography == {
        "canonical_title": "A Paper Title",
        "authors": ["Jane Doe", "John Roe"],
        "venue": "ACL 2025",
        "venue_type": "conference",
        "publication_date": "2025-07-27",
        "doi": "10.1000/example",
        "acl_anthology_id": "2025.acl-long.1",
        "official_url": "https://aclanthology.org/2025.acl-long.1/",
    }
    assert result.enrichment["gateway_request_ids"] == [
        "ready-1",
        "search-1",
        "extract-1",
    ]


def test_ambiguous_candidates_do_not_publish_external_fields() -> None:
    search_results = {
        "results": [
            {
                "title": "A Paper Title",
                "url": "https://example.org/one",
                "content": "A Paper Title",
            },
            {
                "title": "A Paper Title",
                "url": "https://example.net/two",
                "content": "A Paper Title",
            },
        ]
    }
    gateway = FakeGateway(
        response(200, {"status": "ready"}, "ready-1"),
        response(200, search_results, "search-1"),
        response(200, {}, "unused"),
    )

    result = enrich_bibliography("A Paper Title", "", "", gateway)

    assert result.status == "ambiguous"
    assert result.bibliography == {}
    assert result.enrichment["source_url"] is None


def test_unready_gateway_degrades_without_raising() -> None:
    gateway = FakeGateway(
        response(503, {"detail": {"error": "gateway_key_pool_exhausted"}}, "ready-1"),
        response(200, {}, "unused"),
        response(200, {}, "unused"),
    )

    result = enrich_bibliography("A Paper Title", "2025", "ACL", gateway)

    assert result.status == "unavailable"
    assert result.bibliography == {}


def test_acl_anthology_outranks_arxiv_and_parses_definition_list() -> None:
    title = "The Role Of Latent Romanization In Multilinguality In LLMs"
    acl_url = "https://aclanthology.org/2025.findings-acl.1354"
    gateway = FakeGateway(
        response(200, {"status": "ready"}, "ready-1"),
        response(
            200,
            {
                "results": [
                    {"title": title, "url": "https://arxiv.org/abs/2502.07424", "content": "2025"},
                    {"title": title, "url": acl_url, "content": "ACL 2025"},
                ]
            },
            "search-1",
        ),
        response(
            200,
            {
                "results": [
                    {
                        "url": acl_url,
                        "raw_content": """## [RomanLens: The Role Of Latent Romanization In Multilinguality In LLMs](paper.pdf)

[Alan Saji](/people/alan-saji/), [Jaavid Aktar Husain](/people/jaavid/)

Anthology ID:
:   2025.findings-acl.1354

Volume:
:   [Findings of the Association for Computational Linguistics: ACL 2025](/volume)

Month:
:   July

Year:
:   2025

Venue:
:   [Findings](/venues/findings/)

DOI:
:   [10.18653/v1/2025.findings-acl.1354](https://doi.org/example)

PDF:
:   <https://aclanthology.org/2025.findings-acl.1354.pdf>
""",
                    }
                ],
                "failed_results": [],
            },
            "extract-1",
        ),
    )

    result = enrich_bibliography(title, "2025", "ACL", gateway)

    assert result.status == "verified"
    assert result.enrichment["source_url"] == acl_url
    assert result.bibliography["authors"] == ["Alan Saji", "Jaavid Aktar Husain"]
    assert result.bibliography["venue"] == (
        "Findings of the Association for Computational Linguistics: ACL 2025"
    )
    assert result.bibliography["publication_date"] == "2025-07"
    assert result.bibliography["doi"] == "10.18653/v1/2025.findings-acl.1354"
    assert result.bibliography["official_pdf_url"].endswith(".pdf")


def test_arxiv_abs_and_html_candidates_are_one_paper() -> None:
    title = "Language-Specific Latent Process Hinders Cross-Lingual Performance"
    abs_url = "https://arxiv.org/abs/2505.13141"
    gateway = FakeGateway(
        response(200, {"status": "ready"}, "ready-1"),
        response(
            200,
            {
                "results": [
                    {"title": f"[2505.13141] {title}", "url": abs_url, "content": "2025"},
                    {"title": title, "url": "https://arxiv.org/html/2505.13141v3", "content": "2025"},
                ]
            },
            "search-1",
        ),
        response(
            200,
            {
                "results": [
                    {
                        "url": abs_url,
                        "raw_content": f"""# Computer Science > Computation and Language
# Title:{title}
| Cite as: | [arXiv:2505.13141]({abs_url}) [cs.CL] |
""",
                    }
                ],
                "failed_results": [],
            },
            "extract-1",
        ),
    )

    result = enrich_bibliography(title, "2025", "ACL", gateway)

    assert result.status == "verified"
    assert result.enrichment["source_url"] == abs_url
    assert result.bibliography["arxiv_id"] == "2505.13141"
    assert result.bibliography["publication_date"] == "2025-05"


def test_acl_lookup_retries_with_anthology_site_filter() -> None:
    title = "Do Llamas Work in English On the Latent Language of Multilingual Transformers"
    acl_url = "https://aclanthology.org/2024.acl-long.820"

    class RecordingGateway:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def ready(self) -> GatewayResponse:
            return response(200, {"status": "ready"}, "ready-1")

        def search(self, query: str) -> GatewayResponse:
            self.queries.append(query)
            if query.startswith("site:aclanthology.org"):
                return response(
                    200,
                    {
                        "results": [
                            {
                                "title": f"{title} - ACL Anthology",
                                "url": acl_url,
                                "content": "ACL 2024",
                            },
                            {
                                "title": f"{title} - ACL Anthology PDF",
                                "url": f"{acl_url}.pdf",
                                "content": "ACL 2024",
                            },
                        ]
                    },
                    "search-acl",
                )
            return response(
                200,
                {
                    "results": [
                        {
                            "title": f"[2402.10588] {title}",
                            "url": "https://arxiv.org/abs/2402.10588",
                            "content": "2024",
                        }
                    ]
                },
                "search-general",
            )

        def extract(self, urls: list[str]) -> GatewayResponse:
            assert urls == [acl_url]
            return response(
                200,
                {
                    "results": [
                        {
                            "url": acl_url,
                            "raw_content": """# Do Llamas Work in English? On the Latent Language of Multilingual Transformers
Anthology ID:
:   2024.acl-long.820
Volume:
:   Proceedings of the 62nd Annual Meeting of the Association for Computational Linguistics
Month:
:   August
Year:
:   2024
DOI:
:   10.18653/v1/2024.acl-long.820
""",
                        }
                    ]
                },
                "extract-1",
            )

    gateway = RecordingGateway()

    result = enrich_bibliography(title, "2024", "ACL", gateway)

    assert result.status == "verified"
    assert gateway.queries == [
        f'"{title}"',
        f'site:aclanthology.org "{title}"',
    ]
    assert result.enrichment["source_url"] == acl_url
