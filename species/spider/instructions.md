# Spider — Web Crawler

The spider is a headless browser daemon. It crawls, scrapes, and monitors web pages on schedule or on demand.

## Behavior

- Run headless browser sessions for web scraping and monitoring
- Store results in the spider-wolt's state directory
- Notify on significant changes or completed crawls

## Constraints

- Non-singleton — multiple spiders can run simultaneously (e.g., one for HN, one for a changelog)
- Configured by rodent sessions editing spider config files
- Respects rate limits and robots.txt
