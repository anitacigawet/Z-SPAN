// These are the HQ billboard copy slots, rendered verbatim on the two LED
// tickers; edit captions here.

export interface BillboardSlide {
 id: string;
 caption: string;
}

export interface Billboard {
 id: string;
 slides: BillboardSlide[];
}

export const HQ_BILLBOARDS: Billboard[] = [
 {
 id: "upper-band",
 slides: [
 { id: "b1", caption: "BREAKING — Apr 21 City Council transcript now live on Z-SPAN" },
 { id: "b2", caption: "NOW STREAMING — Kingman City Council, gavel-to-gavel" },
 { id: "b3", caption: "11 of 14 council feeds online tonight — coverage holding steady" },
 { id: "b4", caption: "PLACEHOLDER — civic announcement slot · drops in when ops pushes one" },
 { id: "b5", caption: "DO NOT ACQUIESCE — the reluctant acceptance of something without protest" },
 ],
 },
 {
 id: "lower-band",
 slides: [
 { id: "c1", caption: "AGENT FLOOR — Vocabulary Curator resolved 3 disputed quotes · 2h ago" },
 { id: "c2", caption: "PARSER CUSTODIAN escalated — Maricopa minutes format drifted, awaiting review" },
 { id: "c3", caption: "CONTENT SCOUT surfaced 2 new municipal feeds: Flagstaff, Yuma" },
 { id: "c4", caption: "PLACEHOLDER — sponsor / partner acknowledgement slot" },
 ],
 },
];
