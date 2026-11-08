# Recognise Macedonian denar and Bulgarian lev prices

Menus from Skopje and Sofia are coming into the golden set, and their prices are read without a currency:
"350 ден" from a Skopje café comes out as a plain number 350 with no currency, and "12,50 лв" from Sofia
does not parse at all, so the whole price line is lost.

Recognise the two currencies wherever the other Balkan currencies (RSD, BAM) are recognised today:

- Macedonian denar: `ден`, `ден.`, `MKD` (any letter case) → currency code `MKD`.
- Bulgarian lev: `лв`, `лв.`, `BGN` (any letter case) → currency code `BGN`.

Acceptance criteria:

- `parse_price` handles single prices and ranges with these marks, e.g. `350 ден` → 350 MKD,
  `12,50 лв.` → 12.50 BGN, `300-450 ден` → range 300–450 MKD, `от 9 лв` → "from" 9 BGN.
- `find_prices` finds them inside a line, e.g. `Шопска салата ..... 7,90 лв` → 7.90 BGN, including two-price
  pairs such as `120/180 ден`.
- `split_trailing_price` splits `Тавче гравче 280 ден.` into the dish and 280 MKD.
- Nothing that parses today changes its result.
- Tests cover the new currencies.
