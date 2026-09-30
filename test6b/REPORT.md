# Test 6B: opportunity-conditioned consumed-language behaviour

## Disposition

PASS WITH CAVEAT. Reconstruction and reconciliation passed. Qualified consumed-language allocation retains variation within opportunity and activity contexts and merits further testing. This does not establish a statistically or causally independent behavioural dimension.

## Sources and definitions

Read-only MySQL 8.0.46: profiles, subscription_cycles, content_catalogue, content_audio_languages, viewing_sessions, view_events, profile_viewership_window, and existing Test-5 opportunity/activity sidecars. The activity sidecar supplied quintiles only, not mechanism labels.

Canonical opportunity, access, qualification and programme calculations were reused. Manual qualification is watch_seconds >= min(300 seconds, 10% runtime); autoplay qualification is watch_seconds >= min(480 seconds, 20% runtime). Qualified events contribute full watch time. Viewing allocations use event-start windows, not first-start asset ownership.

Opportunity is resolved daily using profile existence, historical subscriptions and canonical catalogue availability. BROAD VOD access is identical for ALL_ACCESS and ALL_ACCESS_SPORTS. For REGIONAL, the pack-language track admits the title and all listed tracks on that entitled title are selectable. Independent audio-track availability dates are absent, so tracks inherit title dates.

Primary consumed-language breadth uses qualified events. The mart's distinct_audio_languages_consumed includes all events; raw breadth is separately retained and reconciled. HHI is the sum of squared language shares, with no added evidence threshold. Opportunity pair/day shares describe supply composition, not expected choice probabilities.

## Validation and population

All 111 recorded checks passed. BASELINE_90 has 8,761 eligible profile-windows and FINAL_90 has 9,762; total 18,523. PARTIAL and MIXED_ACCESS are included. No impossible consumption, consumed breadth above reachable breadth, duplicated language keys, opportunity reconciliation failures, or viewing-denominator reconciliation failures were found. Later plan states did not leak backward. The saved workbook's 5,341,705 data cells were compared with validated frames.

The computation took 183.724 seconds excluding workbook export. The corrected streaming export plus saved-file verification took 104.1 seconds, excluding earlier layout/export attempts.

## Overall results

Window samples are separate populations, not a same-profile longitudinal comparison.

| Measure | BASELINE | FINAL |
|---|---:|---:|
| Mean reachable languages | 9.9781 | 9.8674 |
| Reachable-language range | 9-10 | 9-10 |
| Mean qualified consumed languages | 4.4445 | 4.4344 |
| Median consumed languages | 4 | 4 |
| Consumed-language IQR | 3-6 | 3-6 |
| One-language qualified consumption | 5.22% | 7.87% |
| Mean top consumed-language share | 62.42% | 64.60% |
| Mean top opportunity-language day share | 30.98% | 30.29% |
| Mean consumed-language HHI | 0.5030 | 0.5267 |
| Mean opportunity-language day HHI | 0.1802 | 0.1779 |

Consumption HHI exceeds opportunity HHI for 99.43% and 99.47% of profiles. Finite consumption and title concentration can produce this difference without a distinct language preference effect.

## Access families

| Access | Profiles B/F | Mean consumed breadth B/F | Mean top consumed share B/F |
|---|---:|---:|---:|
| BROAD | 6087/6834 | 4.877/4.858 | 57.30%/59.60% |
| REGIONAL_Bengali | 230/223 | 3.691/3.677 | 72.33%/74.08% |
| REGIONAL_Hindi | 974/1074 | 3.089/3.044 | 79.16%/80.96% |
| REGIONAL_Kannada | 185/197 | 3.503/3.706 | 73.60%/72.66% |
| REGIONAL_Malayalam | 199/219 | 3.513/3.749 | 72.68%/71.21% |
| REGIONAL_Marathi | 225/244 | 3.711/3.455 | 70.21%/74.52% |
| REGIONAL_Tamil | 341/350 | 3.258/3.229 | 73.99%/77.95% |
| REGIONAL_Telugu | 245/298 | 3.331/3.299 | 73.00%/76.13% |
| MIXED_ACCESS | 275/323 | 4.673/4.616 | 62.98%/67.50% |

Across regional pack/window groups, 79.1%-93.3% of profiles meaningfully consumed a language other than the pack language. Pack language is not an observed native language.

## Activity and programme context

Within BROAD, mean consumed breadth rises from watch-hours Q1 to Q5: 2.744 to 7.010 in BASELINE and 2.361 to 7.164 in FINAL. Under separate active-days quintiles it rises from 2.692 to 6.955 and 2.411 to 7.094. BROAD exceeds every regional pack in every quintile under both separate controls in both windows. These coarse strata do not establish causal adjustment.

Among FULL_90 BROAD profiles in watch-hours Q3, opportunity totals were identical within each window, but consumed breadth had IQR 4-6. Top-language share IQRs were approximately 44%-65% and 41%-67%. This supporting subset does not replace the full eligible population.

| Dominant programme | Profiles B/F | Mean consumed breadth B/F |
|---|---:|---:|
| MOVIE | 3978/5474 | 4.830/4.895 |
| WEB_SERIES | 4543/3336 | 4.138/3.802 |
| TV_CATCHUP | 228/867 | 3.741/3.829 |
| REALITY | 10/82 | 6.600/5.817 |
| DOCUMENTARY_SPECIAL | 2/3 | 4.000/4.333 |

Dominance is by qualified minutes. Reality-dominant profiles have high viewing volume; documentary groups are too small for substantive conclusions. Programme selection remains a confounder.

## Consequential asymmetry and limitations

Reachable-language counts saturate at 9-10 but supply depth differs greatly. BROAD English availability comes from only 1-2 titles while Hindi is supported by hundreds. Kannada packs have no English opportunity; FINAL Hindi packs have no Urdu opportunity. A rare-language addition/removal changes raw breadth as much as a deeply represented language. Count ratios therefore conceal supply composition and are diagnostics, not openness or catalogue-utilisation scores.

Raw breadth exceeds qualified breadth for 460 BASELINE and 395 FINAL profiles. The consumption-language extract retains 875 raw-only rows, explicitly flagged; 82,227 of its 83,102 rows have qualified consumption.

No native-language inference, longitudinal comparison, segmentation, headroom claim, openness score, behavioural threshold or mechanism-state integration was performed.

## Recommended next step

For Test 6C, distinguish languages available on selected titles from the audio tracks selected within those titles. Examine programme and activity context before deciding whether language adds an independent fingerprint dimension. This is a recommendation; Test 6C has not started.

## Complete persistence recommendation

After analytical review, consider four analysis-layer sidecars: profile_language_opportunity_window (profile/window), profile_language_opportunity_long (profile/window/reachable language), profile_language_behavior_window (profile/window), and profile_language_behavior_long (profile/window/consumed language). Existing mart overlap does not replace qualified breadth, language HHI, opportunity composition and detailed allocation. Preserve raw versus qualified distinctions. Use a view/join for the merged extract, and retain grouped diagnostics as evidence artifacts. No permanent tables were created.

## Visual recommendations

| Question and relationship | Chart and reason | Contrary evidence |
|---|---|---|
| Broad opportunity versus narrower consumption; distribution | Discrete distributions of both breadth measures | Consumption clusters near opportunity breadth |
| Access gaps across activity; conditional magnitude | Separate quintile point plots with group support | Gaps disappear or consistently reverse |
| Consumption versus opportunity concentration; deviation | Distribution of HHI differences with zero reference | Differences concentrate near zero |
| Programme context; distribution | Supported-group box plots within access regimes | Conditional distributions align |

No charts were created. Full numerical distributions, methods, checks, allocation tables and detailed extracts are included in the workbook and CSVs. Consult the manifest for exact file row counts.
