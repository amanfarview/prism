"""Starter data for the four portcos. Competitor lists and prompts are a first
cut to edit in Command Centre > Brands; they are not research outputs.
"""
from __future__ import annotations

from sqlalchemy import select

from .models import Brand, Competitor, Fact, Market, Prompt, Schedule

PORTCOS = [
    {
        "name": "Amplience", "slug": "amplience", "domain": "amplience.com", "category": "Headless CMS / DAM for commerce",
        "aliases": ["Amplience Dynamic Content", "Amplience Content Hub"],
        "competitors": [("Contentful", "contentful.com", []), ("Adobe Experience Manager", "business.adobe.com", ["AEM"]),
                        ("Sanity", "sanity.io", []), ("Storyblok", "storyblok.com", []),
                        ("Contentstack", "contentstack.com", []), ("Bloomreach", "bloomreach.com", []),
                        ("Sitecore", "sitecore.com", []), ("Optimizely", "optimizely.com", []),
                        ("Hygraph", "hygraph.com", ["GraphCMS"]), ("Builder.io", "builder.io", [])],
        "prompts": [
            ("Best headless CMS for a large fashion retailer", ["Headless CMS", "Retail"]),
            ("Which CMS works best with commercetools for an enterprise storefront?", ["Headless CMS", "Composable"]),
            ("What are the best composable content platforms for ecommerce?", ["Composable"]),
            ("Which content platforms let merchandisers build campaigns without developers?", ["Content ops"]),
            ("Best digital asset management system for a retailer with millions of product images", ["DAM"]),
            ("Which headless CMS is best for multi-brand, multi-region retail?", ["Headless CMS", "Retail"]),
            ("Contentful alternatives for enterprise retail", ["Comparison"]),
            ("Which CMS vendors offer AI tools for writing product content at scale?", ["AI content"]),
            ("Best CMS to pair with Salesforce Commerce Cloud", ["Headless CMS", "Composable"]),
            ("Which content platform can preview a site as it will look on a sale day?", ["Content ops"]),
        ],
    },
    {
        "name": "Unily", "slug": "unily", "domain": "unily.com", "category": "Employee experience platform / intranet",
        "aliases": [],
        "competitors": [("Simpplr", "simpplr.com", []), ("Staffbase", "staffbase.com", []),
                        ("LumApps", "lumapps.com", []), ("Workvivo", "workvivo.com", []),
                        ("Microsoft Viva", "microsoft.com", ["Viva Connections", "Viva Engage"]),
                        ("Interact", "interactsoftware.com", ["Interact Software"]), ("Haiilo", "haiilo.com", []),
                        ("Firstup", "firstup.io", [])],
        "prompts": [
            ("Best intranet software for a large enterprise", ["Intranet"]),
            ("Which employee experience platforms work best with Microsoft 365?", ["Intranet", "M365"]),
            ("Top employee communications platforms for frontline workers", ["Frontline"]),
            ("Simpplr alternatives for a global company", ["Comparison"]),
            ("Which intranet platforms have the best AI features?", ["AI"]),
            ("Best SharePoint intranet replacement", ["Intranet", "M365"]),
            ("What is the most secure enterprise intranet platform?", ["Security"]),
            ("Which digital workplace platforms suit regulated industries like banking?", ["Industry"]),
        ],
    },
    {
        "name": "Exclaimer", "slug": "exclaimer", "domain": "exclaimer.com", "category": "Email signature management",
        "aliases": [],
        "competitors": [("CodeTwo", "codetwo.com", []), ("Letsignit", "letsignit.com", []),
                        ("Rocketseed", "rocketseed.com", []), ("WiseStamp", "wisestamp.com", []),
                        ("Opensense", "opensense.com", []), ("Templafy", "templafy.com", [])],
        "prompts": [
            ("Best email signature management software for Microsoft 365", ["Signatures", "M365"]),
            ("How do I manage email signatures centrally for Google Workspace?", ["Signatures", "Google"]),
            ("Which email signature tools let marketing run banner campaigns?", ["Marketing"]),
            ("CodeTwo vs Exclaimer", ["Comparison"]),
            ("Best email signature software for a company with 5,000 staff", ["Enterprise"]),
            ("Which tools keep email signatures compliant for legal disclaimers?", ["Compliance"]),
        ],
    },
    {
        "name": "Grantify", "slug": "grantify", "domain": "grantify.io", "category": "Grant funding platform (Innovate UK etc.)",
        "aliases": [],
        "competitors": [("TBAT Innovation", "", []), ("Myriad Associates", "", []), ("Ayming", "ayming.co.uk", []),
                        ("Leyton", "leyton.com", []), ("ForrestBrown", "forrestbrown.co.uk", []),
                        ("Kene Partners", "", [])],
        "prompts": [
            ("Who can help me apply for an Innovate UK Smart Grant?", ["Innovate UK"]),
            ("Best grant writing services for UK tech startups", ["Grant writing"]),
            ("How do I find government grants for my UK SME's R&D project?", ["Discovery"]),
            ("Is it worth paying a consultant for an Innovate UK application?", ["Innovate UK"]),
            ("Which platforms help startups win non-dilutive funding in the UK?", ["Funding"]),
            ("Best companies to help with Innovate UK innovation loans", ["Innovate UK"]),
        ],
    },
]


def seed_portcos(s) -> int:
    added = 0
    for pc in PORTCOS:
        if s.scalar(select(Brand).where(Brand.slug == pc["slug"])):
            continue
        b = Brand(name=pc["name"], slug=pc["slug"], domain=pc["domain"], category=pc["category"],
                  aliases=pc["aliases"], is_portco=True)
        s.add(b)
        s.flush()
        for name, dom, al in pc["competitors"]:
            s.add(Competitor(brand_id=b.id, name=name, domain=dom, aliases=al))
        for text, tags in pc["prompts"]:
            s.add(Prompt(brand_id=b.id, text=text, tags=tags, source="manual"))
        s.add(Market(brand_id=b.id, country="GB", language="en"))
        s.add(Schedule(brand_id=b.id, cadence="weekly", weekday=0, hour_utc=3, samples=3, active=False))
        added += 1
    s.flush()
    return added


def seed_example_facts(s) -> None:
    """A couple of illustrative facts for Amplience; replace with the real sheet."""
    b = s.scalar(select(Brand).where(Brand.slug == "amplience"))
    if not b or s.scalar(select(Fact).where(Fact.brand_id == b.id)):
        return
    s.add(Fact(brand_id=b.id, attribute="Headquarters", value="London, UK",
               wrong_patterns=[r"Amplience[^.]{0,60}(headquartered|based) in (San Francisco|New York|Berlin)"],
               source="amplience.com"))
    s.add(Fact(brand_id=b.id, attribute="Product type", value="Composable content and DAM platform for commerce (CMS, DAM, AI content)",
               wrong_patterns=[r"Amplience[^.]{0,40}(is an? )?(e-?commerce platform|shopping cart)"],
               source="amplience.com"))
