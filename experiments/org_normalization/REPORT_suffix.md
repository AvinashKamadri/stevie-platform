# Phase D step 2 — corporate-suffix normalization (read-only)

## Distinct-org reduction

- before (norm_key only) : 28,526
- location rule only     : 25,398   (−3,128, 11.0%)
- suffix rule only       : 27,985   (−541, 1.9%)
- combined (loc+suffix)  : 24,334   (−4,192, 14.7%)

## Additivity check

- location reduction      : 3,128
- suffix reduction        : 541
- sum if independent      : 3,669
- actual combined         : 4,192
- overlap (double-counted): -523  -> ≈ additive

## Suffix-attributable merges

- combined-keys that absorb >=2 distinct location-keys: 994
- of those, merging DIFFERENT legal forms (inc vs llc vs …): 96

### Top 20 suffix merges (by # location-variants absorbed)

- `tata consultancy services`  <= ['Tata Consultancy Services', 'Tata Consultancy Services Inc.', 'TATA Consultancy Services Limited', 'TATA Consultancy Services Ltd']  ⚠ different legal forms
- `cathay life insurance`  <= ['Cathay Life Insurance', 'Cathay Life Insurance Co., Ltd, Taipei, Taiwan', 'Cathay Life Insurance Company, Taipei City, Taiwan', 'Cathay Life Insurance Corp. Ltd.']  ⚠ different legal forms
- `samsung electronics`  <= ['Samsung Electronics', 'Samsung Electronics Co.', 'Samsung Electronics Co., Ltd', 'Samsung Electronics GmbH']  ⚠ different legal forms
- `informatica`  <= ['Informatica', 'Informatica Corporation, Redwood City, California', 'Informatica Inc.', 'Informatica LLC']  ⚠ different legal forms
- `dow jones`  <= ['Dow Jones, New York, USA', 'Dow Jones & Co, Princeton, NJ', 'Dow Jones & Company., Princeton, NJ', 'Dow Jones & Company, Inc., New York, New York']  ⚠ different legal forms
- `icici lombard general insurance`  <= ['ICICI Lombard General Insurance', 'ICICI Lombard General Insurance Co Ltd', 'ICICI Lombard General Insurance Company Limited', 'ICICI Lombard General Insurance Company Ltd']  ⚠ different legal forms
- `cisco systems`  <= ['Cisco Systems', 'Cisco Systems Inc', 'Cisco Systems Pvt Ltd']  ⚠ different legal forms
- `makovsky`  <= ['Makovsky, New York, NY', 'Makovsky & Co, New York, NY', 'Makovsky + Company, New York, NY']  ⚠ different legal forms
- `dhl worldwide express`  <= ['DHL WORLDWIDE EXPRESS', 'DHL Worldwide Express & Company LLC, Muscat, Oman', 'DHL Worldwide Express LLC, Dubai, United Arab Emirates']
- `at t`  <= ['AT&T', 'AT&T Corp., Bedminster, NJ', 'AT&T Inc., Dallas, TX, USA']  ⚠ different legal forms
- `qualcomm`  <= ['Qualcomm', 'Qualcomm Inc., San Diego, CA', 'Qualcomm Incorporated, San Diego, CA']  ⚠ different legal forms
- `xactly`  <= ['Xactly, San Jose, CA', 'Xactly Corp, San Jose, CA', 'Xactly Corporation, San Jose, California, USA']  ⚠ different legal forms
- `bridgeview marketing`  <= ['BridgeView Marketing, Portsmouth, NH', 'BridgeView Marketing Corporation', 'BridgeView Marketing, Inc., Portsmouth, NH']  ⚠ different legal forms
- `epicor software`  <= ['Epicor Software', 'Epicor Software Corp, Austin, TX', 'Epicor Software Corporation, Austin, TX']  ⚠ different legal forms
- `ushealth advisors`  <= ['USHEALTH Advisors, Grapevine, TX, USA', 'USHEALTH Advisors, L.L.C.', 'USHEALTH Advisors, LLC, Grapevine, TX']
- `intralinks`  <= ['IntraLinks', 'IntraLinks, Inc., New York, NY', 'IntraLinks Ltd, London, UK']  ⚠ different legal forms
- `aplicor`  <= ['Aplicor', 'Aplicor Inc., Boca Raton, Florida', 'Aplicor LLC, Boca Raton, Florida, USA']  ⚠ different legal forms
- `pacific life insurance`  <= ['Pacific Life Insurance', 'Pacific Life Insurance Co., Newport Beach, CA', 'Pacific Life Insurance Company']  ⚠ different legal forms
- `e trade financial`  <= ['E*TRADE FINANCIAL, New York, NY', 'E*TRADE FINANCIAL Corp., New York, NY', 'E*TRADE Financial Corporation']  ⚠ different legal forms
- `reading room`  <= ['Reading Room, Singapore', 'Reading Room Ltd, London, United Kingdom', 'Reading Room Pte Ltd, Singapore, Singapore']

### ⚠ Safety — every merge that joins different legal forms (first 25)

- `ace consulting`  <= ['ACE Consulting Company', 'ACE Consulting Company, LLC, Nicholasville, KY']
- `adobe systems`  <= ['Adobe Systems, Inc.', 'Adobe Systems Incorporated, San Mateo, CA']
- `aflac`  <= ['Aflac, Columbus, GA, USA', 'Aflac, Inc., Columbus, GA', 'Aflac Incorporated']
- `aplicor`  <= ['Aplicor', 'Aplicor Inc., Boca Raton, Florida', 'Aplicor LLC, Boca Raton, Florida, USA']
- `at t`  <= ['AT&T', 'AT&T Corp., Bedminster, NJ', 'AT&T Inc., Dallas, TX, USA']
- `axa equitable life insurance`  <= ['AXA Equitable Life Insurance Co., New York, NY', 'AXA Equitable Life Insurance Company, New York']
- `benchmarkportal`  <= ['BenchmarkPortal, Inc., Santa Maria, California', 'BenchmarkPortal, LLC']
- `big picture asia`  <= ['Big Picture Asia', 'Big Picture Asia, Inc.', 'Big Picture Asia, Incorporated']
- `blackboard`  <= ['Blackboard Inc.', 'Blackboard Incorporated, Washington, DC']
- `bridgeview marketing`  <= ['BridgeView Marketing, Portsmouth, NH', 'BridgeView Marketing Corporation', 'BridgeView Marketing, Inc., Portsmouth, NH']
- `cathay life insurance`  <= ['Cathay Life Insurance', 'Cathay Life Insurance Co., Ltd, Taipei, Taiwan', 'Cathay Life Insurance Company, Taipei City, Taiwan', 'Cathay Life Insurance Corp. Ltd.']
- `cisco systems`  <= ['Cisco Systems', 'Cisco Systems Inc', 'Cisco Systems Pvt Ltd']
- `cisco systems india`  <= ['Cisco Systems India, Bangalore, Karnataka', 'Cisco Systems India Pvt Limited', 'Cisco Systems India pvt ltd']
- `cisco systems india private`  <= ['Cisco Systems India Private Limited', 'Cisco Systems India Private Ltd']
- `clinphone`  <= ['ClinPhone', 'ClinPhone Inc., East Windsor, NJ', 'ClinPhone plc, Nottingham, UK']
- `clp holdings`  <= ['CLP Holdings Limited', 'CLP Holdings LTD']
- `clp power hong kong`  <= ['CLP Power Hong Kong Limited, Hong Kong', 'CLP Power Hong Kong Ltd']
- `coldwell banker real estate`  <= ['Coldwell Banker Real Estate', 'Coldwell Banker Real Estate Corporation, Parsippany, NJ', 'Coldwell Banker Real Estate LLC']
- `dani communications`  <= ['Dani Communications', 'Dani Communications. Co.', 'Dani Communications Co., Ltd.']
- `datamatics global services`  <= ['Datamatics Global Services Limited', 'Datamatics Global Services Ltd, Mumbai, Maharashtra']
- `dell technologies`  <= ['Dell Technologies', 'Dell Technologies Inc', 'Dell Technologies Ltd.']
- `dhl express international thailand`  <= ['DHL Express International (Thailand) Limited', 'DHL Express International (Thailand) Ltd., Bangkok, Thailand']
- `dow jones`  <= ['Dow Jones, New York, USA', 'Dow Jones & Co, Princeton, NJ', 'Dow Jones & Company., Princeton, NJ', 'Dow Jones & Company, Inc., New York, New York']
- `e trade financial`  <= ['E*TRADE FINANCIAL, New York, NY', 'E*TRADE FINANCIAL Corp., New York, NY', 'E*TRADE Financial Corporation']
- `eclerx services`  <= ['eClerx Services', 'eClerx Services Limited', 'eClerx Services Ltd.']

## Downstream — fuzzy-comparison workload (entity_candidates)

- entity_candidates (org), measurable : 48,571
- auto-resolved by location rule       : 3,353  (6.9%)
- auto-resolved by combined rule       : 4,722  (9.7%)
- remaining fuzzy workload (combined)  : 43,849  (90.3%)

