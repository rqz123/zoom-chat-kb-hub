# Topic translation

Translate the supplied structured conversation topic into the requested target language.

Rules:

- Translate faithfully without adding, removing, resolving, or inferring information.
- Preserve product names, people names, company names, ticket numbers, model numbers, URLs, dates, and technical identifiers.
- Preserve the meaning and uncertainty level of every statement.
- Keep empty strings and empty lists empty.
- Translate every human-readable field, including action-item descriptions, ordinary role descriptions, and tags.
- Return only the structured output requested by the schema.
- Do not translate or reproduce original Zoom messages; they are not included in the input.
