# Explicit missing-hour research policy

Strict hourly research remains the default. The opt-in v3 setting
`gap_policy="segment"` permits at most 24 missing hours within the observed
calendar span. It does not create zero-volume bars, carry prices forward, or
claim that absent trades were observed.

Each contiguous hourly segment computes features and cost-aware labels
independently. Rolling indicators and EMA state restart at its first observation;
warm-up rows are dropped. Entry/exit labels that would cross the gap are also
dropped. Chronological development, calibration, and final-test purges still
apply after segments are combined in time order.

The immutable run identity includes the policy, exact missing-hour list, its
SHA256 hash, and the calendar-hour count. The pinned comparison loader recomputes
that list from the original dataset, checks the 24-hour cap, and requires at least
8,760 hours of calendar coverage by default. Thus 8,751 real observations plus
nine explicitly missing hours can satisfy calendar coverage, without claiming
8,760 observed candles. Strict candidates still require contiguous actual rows.

Missing-hour provenance must be evaluated separately. Segmenting data does not
prove a gap was a no-trade period, establish profitability, or grant promotion.
