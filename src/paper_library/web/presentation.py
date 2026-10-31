"""Read-only view models for catalog filters and physical document navigation."""
from dataclasses import replace
from datetime import datetime, timezone

from paper_library.structure.frontmatter import appendix_boundary, primary_article, section_label


SORTS = {'publication': '发表时间（新到旧）', 'year': '年份（新到旧）',
         'imported': '最近导入', 'title': '标题 A–Z'}


def catalog_view(snapshot, args):
    filters = {name: args.get(name, '').strip()[:200] for name in ('q', 'subject', 'year', 'venue')}
    sorting = args.get('sort', 'publication')
    if sorting not in SORTS:
        sorting = 'publication'
    query = filters['q'].casefold()
    papers = tuple(p for p in snapshot.papers
                   if (not query or query in ' '.join((p.display_title, p.venue, p.id)).casefold())
                   and (not filters['subject'] or (p.subject or '未分类') == filters['subject'])
                   and (not filters['year'] or p.year == filters['year'])
                   and (not filters['venue'] or p.venue == filters['venue']))
    groups = replace(snapshot, papers=papers).grouped()

    def imported(paper):
        try:
            parsed = datetime.fromisoformat(paper.parsed_at)
            return -(parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
        except (ValueError, OverflowError):
            return float('inf')

    keys = {'year': lambda p: (-(int(p.year) if p.year.isdigit() else -1), p.display_title.casefold()),
            'imported': lambda p: (imported(p), p.display_title.casefold()),
            'title': lambda p: p.display_title.casefold()}
    if sorting in keys:
        groups = tuple((subject, tuple(sorted(rows, key=keys[sorting]))) for subject, rows in groups)
    return dict(groups=groups, paper_count=len(snapshot.papers), filtered_count=len(papers),
                filters=filters, sorting=sorting, sorts=SORTS,
                filtered=any(filters.values()),
                subjects=sorted({p.subject or '未分类' for p in snapshot.papers}, key=str.casefold),
                years=sorted({p.year for p in snapshot.papers if p.year}, reverse=True),
                venues=sorted({p.venue for p in snapshot.papers if p.venue}, key=str.casefold))


def reading_navigation(source, headings, physical_headings=()):
    """Only offer an article jump when its physical identity is unambiguous."""
    article = primary_article(source)
    combined = bool(headings and section_label(headings[0].text) == 'research briefing')
    main = next((h for h in (physical_headings or headings) if article and h.source_line == article.start_line), None)
    if main and not any(h.id == main.id for h in headings):
        headings = tuple(sorted((*headings, main), key=lambda h: h.source_line))
    if not combined and main is None:
        main = next(iter(headings), None)
    appendix = next((h.source_line for h in headings if appendix_boundary(h.text)
                     and (not main or h.source_line > main.source_line)), None)
    buckets = []
    for heading in headings:
        if combined and main and heading.source_line < main.source_line:
            label, key = '研究简报', 'briefing'
        elif appendix and heading.source_line >= appendix:
            label, key = '补充材料', 'supplement'
        else:
            label, key = ('论文正文', 'article') if main else ('本文目录', 'document')
        if not buckets or buckets[-1]['key'] != key:
            buckets.append(dict(key=key, label=label, headings=[], nodes=[], open=key in {'article', 'document'}))
        buckets[-1]['headings'].append(heading)
    for bucket in buckets:
        stack = []
        for heading in bucket['headings']:
            node = dict(heading=heading, children=[])
            while stack and stack[-1]['heading'].level >= heading.level:
                stack.pop()
            (stack[-1]['children'] if stack else bucket['nodes']).append(node)
            stack.append(node)
    return dict(groups=buckets, main=main, combined=combined and main is not None)
