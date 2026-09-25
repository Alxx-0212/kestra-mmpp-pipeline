# Excel export for `Nomor RS` integrity

## Lesson

For DigiPOS CMS deposit history downloads, prefer Excel export over CSV export.

Reason: the browser CSV export can serialize `Nomor RS` as numeric/scientific notation, e.g. `8E+10`, even though the field is a phone-like identifier and should remain a string starting with `08...` where applicable.

Excel export preserves the phone-like text better. The scraper should click `.buttons-excel`, save `.xlsx`, then optionally generate a downstream CSV copy from Excel rows.

## Validation rules

- Table total from `#dataInput_info` remains source of truth.
- Exported data row count must equal table total entries exactly.
- Target date in `Transaction Date` must match requested date.
- Reject if any `Nomor RS` value matches scientific notation regex such as `\d+\.?\d*E\+?\d+`.
- Excel export can include title/filter rows before the real table; normalize by locating header row where first two cells are `No` and `Transaction Date`.

## Test evidence from session

Sample test:

```text
user: 411311_A
date: 2026-06-21
rows: 640
xlsx: created
csv copy: created from Excel rows
scientific Nomor RS: 0
Nomor RS values starting 08: 228
```

Sample preserved values:

```text
08202603310010000017
085349000656
082143053398
081353407799
```
