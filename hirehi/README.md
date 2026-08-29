# HireHi DevOps microproject

Local scraper for HireHi DevOps/infra vacancy searches.

Outputs:
- `output/jobs.json`
- `output/jobs.csv`
- `output/summary.md`

Environment variables:
- `HIREHI_EMAIL`
- `HIREHI_PASSWORD`
- `HIREHI_SEARCH_URL` (optional)
- `HIREHI_OUTDIR` (optional)

Default search URL is the DevOps page used in this session.

The script logs in to HireHi, walks the paginated search results, fetches each job detail page, and classifies the likely application/contact channel as one of:
- `hirehi_internal`
- `telegram`
- `linkedin`
- `external_site`
- `unknown`

The script is conservative: it ignores known site ads/social/footer links and reports any uncertain cases as `unknown` rather than guessing.
