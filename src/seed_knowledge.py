"""
Static knowledge items for the knowledge search endpoint.

This module contains a curated list of references on requirements
engineering standards and prioritisation techniques. The endpoint
``/knowledge/search`` exposes these entries and allows simple text
matching over the title, topic or note.
"""

SOURCES = [
    {
        "topic": "requirements_standard",
        "title": "ISO/IEC/IEEE 29148:2018 Requirements Engineering",
        "url": "https://www.iso.org/standard/72089.html",
        "note": (
            "Defines processes and information items for requirements engineering; "
            "emphasizes unambiguous, verifiable requirements."
        ),
    },
    {
        "topic": "prioritization",
        "title": "WSJF (SAFe): cost of delay / job size",
        "url": "https://framework.scaledagile.com/wsjf",
        "note": ("Economics-driven sequencing model; quantify Cost of Delay where possible."),
    },
    {
        "topic": "prioritization",
        "title": "RICE (Reach, Impact, Confidence, Effort)",
        "url": "https://www.intercom.com/blog/rice-simple-prioritization-for-product-managers/",
        "note": ("Useful when you have reach data; simple model to compare options."),
    },
    {
        "topic": "requirements_standard",
        "title": "IEEE 830: Recommended Practice for Software Requirements Specifications",
        "url": "https://standards.ieee.org/standard/830-1998.html",
        "note": (
            "Classic standard for SRS documents; defines structure and quality "
            "attributes for requirements specifications."
        ),
    },
    {
        "topic": "prioritization",
        "title": "MoSCoW Prioritization Method",
        "url": "https://www.agilebusiness.org/dsdm-project-framework/moscow-prioritisation.html",
        "note": (
            "Must have, Should have, Could have, Won't have; simple categorical "
            "prioritization suitable for timeboxed delivery."
        ),
    },
    {
        "topic": "prioritization",
        "title": "Kano Model for Feature Classification",
        "url": "https://foldingburritos.com/blog/kano-model/",
        "note": (
            "Classifies features as basic, performance, or excitement; helps "
            "understand non-linear relationship between functionality and satisfaction."
        ),
    },
    {
        "topic": "quality_model",
        "title": "ISO 25010: Systems and Software Quality Models",
        "url": "https://www.iso.org/standard/35733.html",
        "note": (
            "Defines eight quality characteristics: functional suitability, "
            "reliability, performance efficiency, usability, security, compatibility, "
            "maintainability, and portability."
        ),
    },
    {
        "topic": "requirements_technique",
        "title": "User Stories and Behaviour-Driven Development (BDD)",
        "url": "https://cucumber.io/docs/bdd/",
        "note": (
            "As a <role> I want <goal> so that <benefit>; combined with "
            "Given-When-Then scenarios for executable specifications."
        ),
    },
    {
        "topic": "traceability",
        "title": "Requirements Traceability Matrix (RTM)",
        "url": "https://www.jamasoftware.com/requirements-management-guide/requirements-traceability",
        "note": (
            "Maps requirements to design, implementation, and test artifacts; "
            "ensures complete coverage and impact analysis for changes."
        ),
    },
    {
        "topic": "decision_records",
        "title": "Architecture Decision Records (ADR)",
        "url": "https://adr.github.io/",
        "note": (
            "Lightweight documents capturing context, decision, and consequences "
            "of significant architecture choices; supports knowledge management."
        ),
    },
]
