"""Phase 3's measure: held-out requests (never the catalog's own examples), each with the operation phrase a
request step would extract, and the entry it means (None: no entry supports it, it must be refused later).
English and French, several kinds of tables, paraphrases and goals. Run by the developer (it loads the models):

    uv run python -m orbitrows.pipeline.operations.evaluation [config]   Recall@K per input (the default model)
"""
CASES = [   # (whole request, extracted phrase, entry or None)
    ("Show the variants whose cost is below a third of the price", "cost below a third of the price",
     "COMPARE_PERCENT_OF_COLUMN"),
    ("Liste les employés dont la prime dépasse 15 % du salaire", "prime dépasse 15 % du salaire",
     "COMPARE_PERCENT_OF_COLUMN"),
    ("Which listings sell for over 4 times what they cost us?", "sell for over 4 times what they cost",
     "COMPARE_MULTIPLE_OF_COLUMN"),
    ("Montre les commandes dont les frais dépassent le double du montant", "frais dépassent le double du montant",
     "COMPARE_MULTIPLE_OF_COLUMN"),
    ("Find the products where we keep less than 25% of the selling price after the cost",
     "keep less than 25% of the selling price after the cost", "COMPARE_SHARE_OF_FIRST"),
    ("Show the departments that have under 5% of their budget left after spending",
     "under 5% of their budget left after spending", "COMPARE_SHARE_OF_FIRST"),
    ("Which items carry a markup above 60% on their cost?", "a markup above 60% on their cost",
     "COMPARE_SHARE_OF_SECOND"),
    ("Affiche les magasins dont le chiffre a progressé de plus de 10 % par rapport à l'an dernier",
     "progressé de plus de 10 % par rapport à l'an dernier", "COMPARE_SHARE_OF_SECOND"),
    ("Show the variants that sell for at least 80 more than they cost", "sell for at least 80 more than they cost",
     "COMPARE_DIFFERENCE"),
    ("Which flights landed more than 30 minutes after the scheduled time?",
     "landed more than 30 minutes after the scheduled time", "COMPARE_DIFFERENCE"),
    ("List the products that weigh more than the average", "weigh more than the average", "COMPARE_TABLE_AVERAGE"),
    ("Quels étudiants ont une note inférieure à la moyenne de la classe ?", "note inférieure à la moyenne",
     "COMPARE_TABLE_AVERAGE"),
    ("Show me the cheapest variant price records, every one at that lowest price", "at that lowest price",
     "COMPARE_TABLE_MINIMUM"),
    ("Quel entrepôt a le stock le plus faible de tous ?", "le stock le plus faible de tous", "COMPARE_TABLE_MINIMUM"),
    ("Who got the top score of the whole exam?", "the top score of the whole exam", "COMPARE_TABLE_MAXIMUM"),
    ("Montre le produit au prix le plus élevé", "au prix le plus élevé", "COMPARE_TABLE_MAXIMUM"),
    ("Which suppliers provide more than five products?", "provide more than five products", "COUNT_LINKED_RECORDS"),
    ("Liste les clients qui n'ont passé qu'une seule commande", "n'ont passé qu'une seule commande",
     "COUNT_LINKED_RECORDS"),
    ("Show the variants with fewer than 20 units in stock overall", "fewer than 20 units in stock overall",
     "SUM_LINKED_RECORDS"),
    ("Quels projets cumulent plus de 500 heures saisies ?", "cumulent plus de 500 heures saisies",
     "SUM_LINKED_RECORDS"),
    ("Show the sales made this past month", "made this past month", "WITHIN_LAST_DAYS"),
    ("Affiche les tickets créés ces 7 derniers jours", "créés ces 7 derniers jours", "WITHIN_LAST_DAYS"),
    ("Delete the logs older than 180 days", "older than 180 days", "OLDER_THAN_DAYS"),
    ("Liste les factures émises il y a plus de deux mois", "émises il y a plus de deux mois", "OLDER_THAN_DAYS"),
    ("Bump the prices up by 6 percent", "up by 6 percent", "INCREASE_PERCENT"),
    ("Augmente les salaires de 2 %", "augmente de 2 %", "INCREASE_PERCENT"),
    ("Give a 20% discount on the winter items", "a 20% discount", "DECREASE_PERCENT"),
    ("Baisse les loyers de 3 pour cent", "baisse de 3 pour cent", "DECREASE_PERCENT"),
    ("Add 15 to every order quantity", "add 15", "ADD_AMOUNT"),
    ("Ajoute 2 jours de congé à chaque employé", "ajoute 2 jours", "ADD_AMOUNT"),
    ("Knock 10 off the price of the clearance items", "knock 10 off", "SUBTRACT_AMOUNT"),
    ("Retire 5 points à toutes les notes", "retire 5 points", "SUBTRACT_AMOUNT"),
    ("Triple the loyalty points", "triple", "MULTIPLY_BY"),
    ("Divise par deux les quantités minimales", "divise par deux", "MULTIPLY_BY"),
    ("Make the sale price the purchase price plus 30", "the purchase price plus 30", "SET_FROM_COLUMN"),
    ("Copie l'adresse de facturation dans l'adresse de livraison", "copie l'adresse de facturation",
     "SET_FROM_COLUMN"),
    ("Show the prices with no more than two decimals", "no more than two decimals", "ROUND_DECIMALS"),
    ("Arrondis les poids à l'unité", "arrondis à l'unité", "ROUND_DECIMALS"),
    ("Round the prices to the closest 10", "to the closest 10", "ROUND_TO_NEAREST"),
    ("Arrondis les durées au quart d'heure le plus proche", "au quart d'heure le plus proche", "ROUND_TO_NEAREST"),
    ("Make sure no price is higher than 999", "no price is higher than 999", "CLAMP_MAX"),
    ("Plafonne les heures supplémentaires à 8", "plafonne à 8", "CLAMP_MAX"),
    ("Never let the stock fall under 2", "never fall under 2", "CLAMP_MIN"),
    ("Aucun salaire ne doit être inférieur à 2000", "ne doit être inférieur à 2000", "CLAMP_MIN"),
    ("Set every status to archived", "set to archived", "SET_VALUE"),
    ("Mets la ville à Casablanca pour ces clients", "mets à Casablanca", "SET_VALUE"),
    ("Where there's no email, write none@example.com", "where there's no email, write", "FILL_EMPTY"),
    ("Complète les tailles manquantes avec M", "complète les tailles manquantes", "FILL_EMPTY"),
    ("Put 'OLD-' in front of every reference", "in front of every reference", "ADD_PREFIX"),
    ("Ajoute le préfixe FR- aux codes", "ajoute le préfixe", "ADD_PREFIX"),
    ("Append ' - discontinued' to the product names", "append to the names", "ADD_SUFFIX"),
    ("Ajoute ' (copie)' à la fin des titres", "à la fin des titres", "ADD_SUFFIX"),
    ("Change every 'Ltd' into 'Limited' in the company names", "change every 'Ltd' into 'Limited'", "REPLACE_TEXT"),
    ("Remplace 'rue' par 'avenue' dans les adresses", "remplace 'rue' par 'avenue'", "REPLACE_TEXT"),
    ("Make the city names all caps", "all caps", "UPPER_CASE"),
    ("Mets les noms en majuscules", "en majuscules", "UPPER_CASE"),
    ("Convert the emails to lowercase", "to lowercase", "LOWER_CASE"),
    ("Écris les tags en minuscules", "en minuscules", "LOWER_CASE"),
    ("Strip the leading and trailing blanks from the codes", "strip the leading and trailing blanks",
     "TRIM_SPACES"),
    ("Enlève les espaces au début et à la fin des noms", "enlève les espaces au début et à la fin", "TRIM_SPACES"),
    ("Push the delivery dates back by 3 days", "push back by 3 days", "SHIFT_DATE"),
    ("Avance les échéances d'un mois", "avance d'un mois", "SHIFT_DATE"),
    ("Wipe the phone numbers of the deleted accounts", "wipe the phone numbers", "CLEAR_VALUE"),
    ("Vide le champ remarque", "vide le champ", "CLEAR_VALUE"),
    # no entry supports these: they must be refused, never matched to a close one
    ("Show the variants priced above the median", "above the median", None),
    ("Find the 10% most expensive products", "the 10% most expensive", None),
    ("Which salaries are more than two standard deviations from the mean?", "two standard deviations from the mean",
     None),
    ("Show the 90th percentile of delivery times", "90th percentile", None),
    ("Rank the stores by revenue", "rank by revenue", None),
    ("Remove the duplicate customers", "remove the duplicates", None),
    ("Split the full name into first and last name", "split into first and last name", None),
    ("Convert the prices from dollars to euros at today's rate", "convert at today's rate", None),
    ("Translate the descriptions into Spanish", "translate into Spanish", None),
    ("Fix the spelling mistakes in the titles", "fix the spelling mistakes", None),
    ("Show the running total of sales by day", "running total by day", None),
    ("Calcule la variance des notes", "la variance des notes", None),
    ("Trouve les produits dont le nom ressemble à celui d'un autre", "nom ressemble à celui d'un autre", None),
    ("Show the sales from last quarter", "from last quarter", None),
    ("Encrypt the passwords", "encrypt", None),
    ("Predict next month's sales", "predict next month", None),
]


def measure(index, field: int) -> dict:
    """Recall@1/3/5/10 on the supported cases, and the best similarity of each refused one next to the right
    entry's similarity on the supported ones (the gap a rejection threshold would need)."""
    hits, right, refused = {1: 0, 3: 0, 5: 0, 10: 0}, [], []
    supported = [c for c in CASES if c[2]]
    for c in CASES:
        found = index.retrieve(c[field], 10)
        if c[2] is None:
            refused.append(found[0][1])
            continue
        ranks = [i for i, (op, _) in enumerate(found) if op == c[2]]
        for k in hits:
            hits[k] += bool(ranks and ranks[0] < k)
        right.append(next((s for op, s in found if op == c[2]), 0.0))
    return {f"R@{k}": round(v / len(supported), 2) for k, v in hits.items()} | {
        "right median": round(sorted(right)[len(right) // 2], 2), "refused max": round(max(refused), 2),
        "refused median": round(sorted(refused)[len(refused) // 2], 2)}


if __name__ == "__main__":
    import sys
    from .catalog import PATH, connect
    from .index import DEFAULT, Index, embed
    db = connect(PATH, write=True)
    for config_id in sys.argv[1:] or [DEFAULT]:              # another model: measured, and its vectors stored
        print(config_id, "entries embedded:", embed(db, config_id))
        index = Index(db, config_id)
        print("stale:", index.stale)
        for field, name in ((0, "request"), (1, "phrase")):
            print(f"{config_id:11} {name:8}", measure(index, field))
