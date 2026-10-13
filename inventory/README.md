# frdiplomacy.org archive

Captured 2026-10-13T18:42:46.034879+00:00.

Read-only catalog of the Squarespace 7.1 site. Nothing on the live site was changed, and no Squarespace account was used.

## What was captured

- URLs recorded: 260
- Sitemap URLs: 25
- Content pages: 31 files (16 without a trailing-slash twin in the filename, including both `/` and `/home`)
- Content posts: 24 files (12 canonical posts, plus a trailing-slash file for each)
- Assets referenced: 63 (63 downloaded, 0 failed)
- Redirecting URLs: 155
- External links checked: 108 (6 broken or unreachable)

## Method

1. Fetched `https://frdiplomacy.org/sitemap.xml` (it redirects to `https://www.frdiplomacy.org/sitemap.xml`) and recorded every `<loc>`.
2. Crawled internal links from the homepage HTML, footer, header, and blog bodies. Probed the paths listed in `scripts/archive_site.py` (`/frd-method`, `/cart`, commerce paths, and the homepage aliases). Requested trailing-slash, apex-host, and http variants and stored the redirect chain.
3. Fetched `/blog?format=json-pretty` and `/blog?offset=12&format=json-pretty`, plus `/blog?format=rss` when the crawl reached it, to enumerate public posts.
4. Saved each page's rendered HTML under `raw/html/` and its `?format=json-pretty` payload under `raw/json/`.
5. Rendered markdown with Firecrawl (`--only-main-content`). Blog post bodies prefer the JSON `item.body` HTML.
6. Wrote one markdown file per 200 page and post under `content/`, with the original wording. Asset files use the decoded original filename.
7. Rate limit: one request per second.

Squarespace 7.1 page JSON `mainContent` is an empty layout wrapper. Page copy comes from the rendered HTML. Post copy comes from the JSON body.

Dates in frontmatter are converted from Squarespace millisecond timestamps in the site timezone `America/Los_Angeles`. A field that was not in the payload is `null`.

## What failed

60 failure log lines are in `inventory/failures.jsonl` and copied in `inventory/issues.md`.

- {"step": "capture", "url": "https://www.frdiplomacy.org/cart?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:26.384139+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/frd-method?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:27.319275+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/search?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:29.383179+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/method?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:30.260613+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/the-frd-method?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:31.224270+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/commerce?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:32.330429+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/store?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:33.330800+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/shop?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:34.445694+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/products?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:35.329470+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/donate?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:36.315420+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/faq?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:37.307510+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/faqs?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:38.394763+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/privacy?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:39.309416+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/privacy-policy?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:40.498261+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/terms?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:41.375296+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/terms-of-service?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:42.325746+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/account?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:43.193428+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/login?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:44.392301+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/news?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:45.328903+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/journal?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:46.353314+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/events?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:47.354103+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/partners?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:48.393261+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/resources?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:49.428001+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/blog/july-19-post-who-blogs-here?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:28:59.490191+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/blog/july-19-post-1?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:29:01.852842+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/blog/july-19-post-what-about-extremists?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:29:03.893632+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/blog/july-19-post-power-shared-meal?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:29:05.878272+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/blog/july-19-post-friendship-and-trusted?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:29:07.946885+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/cart/?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:29:20.036106+00:00"}
- {"step": "capture", "url": "https://www.frdiplomacy.org/frd-method/?format=json-pretty", "error": "not json: Expecting value: line 1 column 1 (char 0)", "at": "2026-10-13T18:29:20.901019+00:00"}
- … 30 more
