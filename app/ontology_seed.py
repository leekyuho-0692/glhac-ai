"""임계원재료 ontology seed — 설계 24.13/24.13.8 (illustrative, 전문가 검수 전).
carrier_check: 'alcohol'(향료 등 알코올 용매 점검) | 'gelatin'(카로틴 등 젤라틴 캐리어 점검)."""
from .models import IngredientOntology, RuleVersion

RULE_VERSION = "BPJPH-2026-01"


def _e(uid, name, cat, status, sev, sources, aliases, evidence=None, alts=None,
       e_number=None, najis=False, carrier=None):
    return dict(ingredient_uid=uid, canonical_name=name, category=cat, e_number=e_number,
                default_status=status, severity=sev, najis_risk=najis, carrier_check=carrier,
                sources=sources, aliases=aliases, required_evidence=evidence or [],
                alternatives=alts or [], rule_version=RULE_VERSION)


ONTOLOGY = [
    # ── 동물성 (haram) ──
    _e("ing.lard", "Lard", "animal_protein", "haram", "high", ["animal"],
       {"id": ["lemak babi"], "ko": ["돼지기름", "라드"], "en": ["lard"]}, najis=True,
       evidence=["reformulation"], alts=["식물성 쇼트닝"]),
    _e("ing.pork_gelatin", "Pork gelatin", "animal_protein", "haram", "high", ["animal"],
       {"id": ["gelatin babi"], "ko": ["돼지젤라틴"], "en": ["pork gelatin"]}, najis=True,
       evidence=["reformulation"], alts=["fish gelatin", "agar"]),
    _e("ing.ethanol_beverage", "Alcoholic beverage", "alcohol", "haram", "high", ["ferment"],
       {"id": ["khamr", "minuman beralkohol"], "ko": ["주류", "음용알코올"],
        "en": ["wine", "beer", "rum", "alcohol beverage", "ethanol beverage"]},
       evidence=["reformulation"]),
    # ── 동물성 (mushbooh) ──
    _e("ing.gelatin", "Gelatin", "animal_protein", "mushbooh", "high", ["animal"],
       {"id": ["gelatin"], "ko": ["젤라틴"], "en": ["gelatin", "gelatine"]}, e_number="E441",
       evidence=["halal_cert", "animal_source_declaration"], alts=["fish gelatin", "agar", "pectin"]),
    _e("ing.collagen", "Collagen", "animal_protein", "mushbooh", "high", ["animal"],
       {"id": ["kolagen"], "ko": ["콜라겐"], "en": ["collagen"]},
       evidence=["halal_cert", "source_declaration"], alts=["fish collagen"]),
    _e("ing.tallow", "Tallow", "animal_protein", "mushbooh", "high", ["animal"],
       {"id": ["lemak hewani"], "ko": ["우지", "동물성지방"], "en": ["tallow", "animal fat"]},
       evidence=["halal_slaughter_cert"], alts=["식물성유"]),
    _e("ing.whey", "Whey", "ferment", "mushbooh", "medium", ["animal", "ferment"],
       {"ko": ["유청"], "en": ["whey"]}, evidence=["rennet_source_declaration"], alts=["미생물 rennet whey"]),
    # ── 효소 ──
    _e("ing.rennet", "Rennet", "enzyme", "mushbooh", "high", ["animal", "microbial"],
       {"id": ["renet"], "ko": ["레닛"], "en": ["rennet", "renin"]},
       evidence=["source_declaration"], alts=["microbial rennet"]),
    _e("ing.enzyme", "Enzyme (unspecified)", "enzyme", "mushbooh", "high", ["animal", "microbial"],
       {"id": ["enzim"], "ko": ["효소"], "en": ["enzyme"]},
       evidence=["enzyme_source_declaration"], alts=["microbial enzyme"]),
    _e("ing.lipase", "Lipase", "enzyme", "mushbooh", "medium", ["animal", "microbial"],
       {"ko": ["리파아제"], "en": ["lipase"]}, evidence=["source_declaration"], alts=["microbial lipase"]),
    _e("ing.pepsin", "Pepsin", "enzyme", "mushbooh", "high", ["animal"],
       {"ko": ["펩신"], "en": ["pepsin"]}, evidence=["source_declaration"], alts=["microbial"]),
    # ── 유화제·첨가물 (E-number) ──
    _e("ing.mono_diglycerides", "Mono/Diglycerides", "emulsifier", "mushbooh", "medium",
       ["animal", "plant"], {"ko": ["글리세라이드"], "en": ["mono and diglycerides", "monoglyceride"]},
       e_number="E471", evidence=["source_declaration"], alts=["식물성 E471"]),
    _e("ing.datem", "DATEM", "emulsifier", "mushbooh", "medium", ["animal", "plant"],
       {"en": ["datem"]}, e_number="E472e", evidence=["source_declaration"], alts=["식물성"]),
    _e("ing.lecithin", "Lecithin", "emulsifier", "mushbooh", "medium", ["soy", "egg", "animal"],
       {"ko": ["레시틴"], "en": ["lecithin"]}, e_number="E322",
       evidence=["source_declaration"], alts=["soy lecithin"]),
    _e("ing.glycerin", "Glycerin/Glycerol", "emulsifier", "mushbooh", "medium",
       ["animal", "plant", "synthetic"], {"ko": ["글리세린", "글리세롤"], "en": ["glycerin", "glycerol"]},
       e_number="E422", evidence=["source_declaration"], alts=["식물성/합성 글리세린"]),
    _e("ing.stearic_acid", "Stearic acid", "emulsifier", "mushbooh", "medium", ["animal", "plant"],
       {"ko": ["스테아르산"], "en": ["stearic acid"]}, e_number="E570",
       evidence=["source_declaration"], alts=["식물성 스테아르산"]),
    _e("ing.mg_stearate", "Magnesium stearate", "emulsifier", "mushbooh", "medium", ["animal", "plant"],
       {"ko": ["스테아린산마그네슘"], "en": ["magnesium stearate"]}, e_number="E470b",
       evidence=["source_declaration"], alts=["식물성"]),
    _e("ing.ssl", "Sodium stearoyl lactylate", "emulsifier", "mushbooh", "medium", ["animal", "plant"],
       {"en": ["sodium stearoyl lactylate", "ssl"]}, e_number="E481",
       evidence=["source_declaration"], alts=["식물성"]),
    _e("ing.sorbitan_ester", "Sorbitan esters", "emulsifier", "mushbooh", "medium", ["animal", "plant"],
       {"ko": ["솔비탄"], "en": ["sorbitan", "polysorbate", "sorbitan ester", "tween"]}, e_number="E491",
       evidence=["source_declaration"], alts=["식물성 지방산"]),
    _e("ing.glycine", "Glycine", "additive", "mushbooh", "medium", ["animal", "synthetic"],
       {"ko": ["글리신"], "en": ["glycine"]}, e_number="E640",
       evidence=["source_declaration"], alts=["합성 glycine"]),
    _e("ing.bone_phosphate", "Bone phosphate", "additive", "mushbooh", "high", ["animal"],
       {"id": ["fosfat tulang"], "ko": ["골인산"], "en": ["bone phosphate"]}, e_number="E542",
       evidence=["source_declaration"], alts=["광물 인산염"]),
    _e("ing.inosinate", "Disodium inosinate", "flavor_enhancer", "mushbooh", "medium",
       ["fish", "animal", "yeast"], {"ko": ["이노신산이나트륨"], "en": ["disodium inosinate"]},
       e_number="E631", evidence=["source_declaration"], alts=["효모 유래"]),
    _e("ing.ribonucleotide", "Disodium ribonucleotides", "flavor_enhancer", "mushbooh", "medium",
       ["fish", "animal", "yeast"], {"en": ["disodium ribonucleotides", "ribonucleotide"]},
       e_number="E635", evidence=["source_declaration"], alts=["효모 유래"]),
    # ── 곤충·표면처리 ──
    _e("ing.carmine", "Carmine/Cochineal", "colorant", "mushbooh", "medium", ["insect"],
       {"ko": ["카민", "코치닐"], "en": ["carmine", "cochineal"]}, e_number="E120",
       evidence=["source_declaration"], alts=["식물성 색소"]),
    _e("ing.shellac", "Shellac", "glazing", "mushbooh", "low", ["insect"],
       {"ko": ["셸락"], "en": ["shellac"]}, e_number="E904",
       evidence=["source_declaration"], alts=["식물성 코팅"]),
    _e("ing.beeswax", "Beeswax", "glazing", "mushbooh", "low", ["insect"],
       {"ko": ["밀랍"], "en": ["beeswax"]}, e_number="E901",
       evidence=["source_declaration"], alts=["카나우바 왁스"]),
    _e("ing.lanolin", "Lanolin", "processing_aid", "mushbooh", "low", ["animal"],
       {"ko": ["라놀린"], "en": ["lanolin"]}, e_number="E913",
       evidence=["source_declaration"], alts=["식물성 왁스"]),
    _e("ing.carbon_color", "Vegetable/animal carbon", "colorant", "mushbooh", "medium", ["plant", "animal"],
       {"ko": ["식물성탄소", "카본블랙"], "en": ["vegetable carbon", "carbon black"]}, e_number="E153",
       evidence=["process_declaration"], alts=["식물성 carbon"]),
    # ── 가공보조제·캡슐 ──
    _e("ing.l_cysteine", "L-Cysteine", "processing_aid", "mushbooh", "high", ["animal", "synthetic"],
       {"ko": ["시스테인", "엘시스테인"], "en": ["l-cysteine", "cysteine"]}, e_number="E920",
       evidence=["source_declaration"], alts=["합성 cysteine"]),
    _e("ing.gelatin_capsule", "Gelatin capsule", "processing_aid", "mushbooh", "high", ["animal"],
       {"id": ["kapsul gelatin"], "ko": ["젤라틴캡슐"], "en": ["gelatin capsule"]},
       evidence=["halal_cert"], alts=["HPMC 식물성 캡슐"]),
    _e("ing.shortening", "Shortening", "processing_aid", "mushbooh", "medium", ["animal", "plant"],
       {"ko": ["쇼트닝"], "en": ["shortening"]}, evidence=["source_declaration"], alts=["식물성 쇼트닝"]),
    _e("ing.bone_char", "Bone char", "processing_aid", "mushbooh", "medium", ["animal"],
       {"ko": ["골탄"], "en": ["bone char"]}, evidence=["process_declaration"], alts=["활성탄"]),
    # ── 향료·캐리어 점검 (carrier_check) ──
    _e("ing.flavor", "Flavor/Fragrance", "flavor", "mushbooh", "medium", ["unknown"],
       {"id": ["perisa", "pewangi"], "ko": ["향료", "착향료"], "en": ["flavor", "flavour", "fragrance", "aroma"]},
       carrier="alcohol", evidence=["alcohol_carrier_check", "composition_breakdown"],
       alts=["알코올프리 향료", "PG 캐리어 향료"]),
    _e("ing.vanilla_extract", "Vanilla extract", "flavor", "mushbooh", "medium", ["plant", "ferment"],
       {"ko": ["바닐라추출물", "바닐라익스트랙"], "en": ["vanilla extract"]},
       carrier="alcohol", evidence=["alcohol_carrier_check"], alts=["알코올프리 바닐라"]),
    _e("ing.carotene", "Carotene (color)", "colorant", "halal", "low", ["plant"],
       {"ko": ["카로틴", "베타카로틴"], "en": ["carotene", "beta-carotene"]}, e_number="E160a",
       carrier="gelatin", evidence=["carrier_check"], alts=["식물성 캐리어 카로틴"]),
    # ── 발효·일반 (halal) ──
    _e("ing.citric_acid", "Citric acid", "ferment", "halal", "low", ["ferment"],
       {"ko": ["구연산", "시트르산"], "en": ["citric acid"]}, e_number="E330"),
    _e("ing.lactic_acid", "Lactic acid", "ferment", "halal", "low", ["ferment"],
       {"ko": ["젖산", "락트산"], "en": ["lactic acid"]}, e_number="E270"),
    _e("ing.msg", "MSG", "ferment", "halal", "low", ["ferment"],
       {"ko": ["msg", "글루탐산나트륨"], "en": ["monosodium glutamate", "msg"]}, e_number="E621"),
    _e("ing.water", "Water", "base", "halal", "low", ["mineral"],
       {"id": ["air"], "ko": ["정제수", "물"], "en": ["water", "aqua", "purified water"]}),
    _e("ing.butylene_glycol", "Butylene glycol", "base", "halal", "low", ["synthetic"],
       {"ko": ["부틸렌글라이콜", "부틸렌글리콜"], "en": ["butylene glycol"]}),
    _e("ing.propylene_glycol", "Propylene glycol", "base", "halal", "low", ["synthetic"],
       {"ko": ["프로필렌글라이콜"], "en": ["propylene glycol", "pg"]}, alts=[]),
    _e("ing.salt", "Salt", "base", "halal", "low", ["mineral"],
       {"ko": ["소금", "정제염"], "en": ["salt", "sodium chloride"]}),
    _e("ing.sugar", "Sugar", "base", "halal", "low", ["plant"],
       {"ko": ["설탕", "정제당"], "en": ["sugar", "sucrose"]},
       evidence=["bone_char_free_declaration"]),
]


# ── E-number 전수 확장 (24.13.8) — 대다수 halal, 임계만 별도 플래그 ──
_BULK_HALAL_E = [
    ("E100", "Curcumin"), ("E101", "Riboflavin"), ("E102", "Tartrazine"), ("E110", "Sunset Yellow"),
    ("E122", "Azorubine"), ("E124", "Ponceau 4R"), ("E129", "Allura Red"), ("E132", "Indigotine"),
    ("E133", "Brilliant Blue"), ("E140", "Chlorophyll"), ("E150a", "Caramel"), ("E160b", "Annatto"),
    ("E160c", "Paprika Extract"), ("E161b", "Lutein"), ("E162", "Beetroot Red"), ("E163", "Anthocyanins"),
    ("E170", "Calcium Carbonate"), ("E171", "Titanium Dioxide"), ("E172", "Iron Oxides"),
    ("E200", "Sorbic Acid"), ("E202", "Potassium Sorbate"), ("E211", "Sodium Benzoate"),
    ("E220", "Sulphur Dioxide"), ("E250", "Sodium Nitrite"), ("E260", "Acetic Acid"), ("E296", "Malic Acid"),
    ("E300", "Ascorbic Acid"), ("E301", "Sodium Ascorbate"), ("E325", "Sodium Lactate"),
    ("E331", "Sodium Citrate"), ("E338", "Phosphoric Acid"), ("E341", "Calcium Phosphate"),
    ("E400", "Alginic Acid"), ("E406", "Agar"), ("E407", "Carrageenan"), ("E410", "Locust Bean Gum"),
    ("E412", "Guar Gum"), ("E414", "Gum Arabic"), ("E415", "Xanthan Gum"), ("E420", "Sorbitol"),
    ("E421", "Mannitol"), ("E440", "Pectin"), ("E450", "Diphosphates"), ("E460", "Cellulose"),
    ("E466", "Carboxymethylcellulose"), ("E500", "Sodium Carbonate"), ("E551", "Silicon Dioxide"),
    ("E575", "Glucono Delta Lactone"), ("E1400", "Dextrin"), ("E1422", "Modified Starch"),
]
_BULK_HALAL_E += [
    # 색소 (합성/식물/광물)
    ("E104", "Quinoline Yellow"), ("E131", "Patent Blue V"), ("E141", "Copper Chlorophyllin"),
    ("E142", "Green S"), ("E151", "Brilliant Black BN"), ("E155", "Brown HT"),
    ("E160a", "Beta-carotene (synthetic)"), ("E160d", "Lycopene"), ("E160e", "Beta-apo-8-carotenal"),
    ("E161g", "Canthaxanthin"), ("E164", "Saffron"), ("E173", "Aluminium"), ("E174", "Silver"),
    ("E175", "Gold"), ("E180", "Litholrubine BK"), ("E181", "Tannic Acid"),
    # 보존료
    ("E201", "Sodium Sorbate"), ("E203", "Calcium Sorbate"), ("E210", "Benzoic Acid"),
    ("E212", "Potassium Benzoate"), ("E213", "Calcium Benzoate"), ("E214", "Ethylparaben"),
    ("E218", "Methylparaben"), ("E221", "Sodium Sulphite"), ("E222", "Sodium Bisulphite"),
    ("E223", "Sodium Metabisulphite"), ("E224", "Potassium Metabisulphite"), ("E228", "Potassium Bisulphite"),
    ("E234", "Nisin"), ("E235", "Natamycin"), ("E242", "Dimethyl Dicarbonate"),
    ("E251", "Sodium Nitrate"), ("E252", "Potassium Nitrate"), ("E280", "Propionic Acid"),
    ("E281", "Sodium Propionate"), ("E282", "Calcium Propionate"), ("E283", "Potassium Propionate"),
    ("E290", "Carbon Dioxide"), ("E297", "Fumaric Acid"),
    # 산화방지제·산도조절
    ("E302", "Calcium Ascorbate"), ("E307", "Alpha-tocopherol (synthetic)"), ("E310", "Propyl Gallate"),
    ("E315", "Erythorbic Acid"), ("E316", "Sodium Erythorbate"), ("E319", "TBHQ"), ("E320", "BHA"),
    ("E321", "BHT"), ("E326", "Potassium Lactate"), ("E327", "Calcium Lactate"), ("E332", "Potassium Citrate"),
    ("E333", "Calcium Citrate"), ("E334", "Tartaric Acid"), ("E335", "Sodium Tartrate"),
    ("E336", "Potassium Tartrate"), ("E337", "Potassium Sodium Tartrate"), ("E339", "Sodium Phosphate"),
    ("E340", "Potassium Phosphate"), ("E350", "Sodium Malate"), ("E351", "Potassium Malate"),
    ("E352", "Calcium Malate"), ("E363", "Succinic Acid"), ("E380", "Triammonium Citrate"),
    ("E385", "Calcium Disodium EDTA"),
    # 증점·안정제 (식물/합성)
    ("E401", "Sodium Alginate"), ("E402", "Potassium Alginate"), ("E403", "Ammonium Alginate"),
    ("E404", "Calcium Alginate"), ("E405", "Propylene Glycol Alginate"), ("E413", "Tragacanth"),
    ("E416", "Karaya Gum"), ("E417", "Tara Gum"), ("E418", "Gellan Gum"), ("E425", "Konjac"),
    ("E461", "Methylcellulose"), ("E463", "Hydroxypropylcellulose"), ("E464", "HPMC"),
    ("E465", "Ethylmethylcellulose"), ("E508", "Potassium Chloride"), ("E509", "Calcium Chloride"),
    ("E511", "Magnesium Chloride"), ("E514", "Sodium Sulphate"), ("E516", "Calcium Sulphate"),
    ("E524", "Sodium Hydroxide"), ("E525", "Potassium Hydroxide"), ("E526", "Calcium Hydroxide"),
    ("E528", "Magnesium Hydroxide"), ("E529", "Calcium Oxide"), ("E530", "Magnesium Oxide"),
    ("E535", "Sodium Ferrocyanide"), ("E536", "Potassium Ferrocyanide"), ("E541", "Sodium Aluminium Phosphate"),
    ("E552", "Calcium Silicate"), ("E553", "Talc/Magnesium Silicate"), ("E558", "Bentonite"),
    ("E559", "Kaolin"), ("E574", "Gluconic Acid"), ("E576", "Sodium Gluconate"), ("E578", "Calcium Gluconate"),
    # 향미증진 (발효/합성 글루탐산 계열)
    ("E620", "Glutamic Acid"), ("E622", "Monopotassium Glutamate"), ("E623", "Calcium Diglutamate"),
    ("E624", "Monoammonium Glutamate"), ("E625", "Magnesium Diglutamate"),
    # 감미료
    ("E950", "Acesulfame K"), ("E951", "Aspartame"), ("E952", "Cyclamate"), ("E953", "Isomalt"),
    ("E954", "Saccharin"), ("E955", "Sucralose"), ("E957", "Thaumatin"), ("E960", "Steviol Glycosides"),
    ("E961", "Neotame"), ("E965", "Maltitol"), ("E966", "Lactitol"), ("E967", "Xylitol"),
    ("E968", "Erythritol"),
    # 가스·기타·전분
    ("E938", "Argon"), ("E939", "Helium"), ("E941", "Nitrogen"), ("E942", "Nitrous Oxide"),
    ("E948", "Oxygen"), ("E999", "Quillaia Extract"), ("E1200", "Polydextrose"),
    ("E1404", "Oxidized Starch"), ("E1412", "Distarch Phosphate"), ("E1420", "Acetylated Starch"),
    ("E1442", "Hydroxypropyl Distarch Phosphate"), ("E1450", "Starch Sodium Octenyl Succinate"),
    ("E1505", "Triethyl Citrate"), ("E1520", "Propylene Glycol"),
]
_BULK_MUSHBOOH_E = [
    ("E304", "Ascorbyl Palmitate", ["animal", "plant"], "low"),
    ("E306", "Tocopherol (mixed)", ["animal", "plant"], "low"),
    ("E430", "Polyoxyethylene Stearate", ["animal", "plant"], "medium"),
    ("E431", "Polyoxyethylene (40) Stearate", ["animal", "plant"], "medium"),
    ("E432", "Polysorbate 20", ["animal", "plant"], "medium"),
    ("E433", "Polysorbate 80", ["animal", "plant"], "medium"),
    ("E434", "Polysorbate 40", ["animal", "plant"], "medium"),
    ("E435", "Polysorbate 60", ["animal", "plant"], "medium"),
    ("E436", "Polysorbate 65", ["animal", "plant"], "medium"),
    ("E442", "Ammonium Phosphatides", ["animal", "plant"], "medium"),
    ("E445", "Glycerol Esters of Wood Rosin", ["animal", "plant"], "medium"),
    ("E472a", "Acetic Acid Esters of Mono/Diglycerides", ["animal", "plant"], "medium"),
    ("E472b", "Lactic Acid Esters of Mono/Diglycerides", ["animal", "plant"], "medium"),
    ("E472c", "Citric Acid Esters of Mono/Diglycerides", ["animal", "plant"], "medium"),
    ("E472e", "Mono/Diacetyltartaric Acid Esters (DATEM)", ["animal", "plant"], "medium"),
    ("E473", "Sucrose Esters of Fatty Acids", ["animal", "plant"], "medium"),
    ("E474", "Sucroglycerides", ["animal", "plant"], "medium"),
    ("E475", "Polyglycerol Esters of Fatty Acids", ["animal", "plant"], "medium"),
    ("E476", "Polyglycerol Polyricinoleate (PGPR)", ["animal", "plant"], "medium"),
    ("E477", "Propylene Glycol Esters of Fatty Acids", ["animal", "plant"], "medium"),
    ("E482", "Calcium Stearoyl-2-Lactylate", ["animal", "plant"], "medium"),
    ("E483", "Stearyl Tartrate", ["animal", "plant"], "medium"),
    ("E492", "Sorbitan Tristearate", ["animal", "plant"], "medium"),
    ("E570b", "Magnesium Stearate", ["animal", "plant"], "medium"),
    ("E572", "Magnesium Stearate", ["animal", "plant"], "medium"),
    ("E627", "Disodium Guanylate", ["animal", "microbial"], "medium"),
    ("E628", "Dipotassium Guanylate", ["animal", "microbial"], "medium"),
    ("E630", "Inosinic Acid", ["animal", "microbial"], "medium"),
    ("E632", "Dipotassium Inosinate", ["animal", "microbial"], "medium"),
    ("E633", "Calcium Inosinate", ["animal", "microbial"], "medium"),
    ("E634", "Calcium 5'-Ribonucleotides", ["animal", "microbial"], "medium"),
    ("E921", "L-Cystine", ["animal", "synthetic"], "high"),
    ("E1105", "Lysozyme (egg)", ["animal"], "medium"),
]
_BULK_HARAM_E = [
    ("E1000", "Cholic Acid (bovine bile)", ["animal"]),
]
_seen_uids = {r["ingredient_uid"] for r in ONTOLOGY}
for _en, _nm in _BULK_HALAL_E:
    _u = f"ing.{_en.lower()}"
    if _u in _seen_uids:
        continue
    _seen_uids.add(_u)
    ONTOLOGY.append(_e(_u, _nm, "additive", "halal", "low",
                       ["plant", "synthetic", "mineral"], {"en": [_nm.lower()]}, e_number=_en))
for _en, _nm, _src, _sev in _BULK_MUSHBOOH_E:
    _u = f"ing.{_en.lower()}"
    if _u in _seen_uids:
        continue
    _seen_uids.add(_u)
    ONTOLOGY.append(_e(_u, _nm, "additive", "mushbooh", _sev, _src,
                       {"en": [_nm.lower().split(" (")[0]]}, e_number=_en,
                       evidence=["source_declaration", "halal_cert"]))
for _en, _nm, _src in _BULK_HARAM_E:
    _u = f"ing.{_en.lower()}"
    if _u in _seen_uids:
        continue
    _seen_uids.add(_u)
    ONTOLOGY.append(_e(_u, _nm, "additive", "haram", "high", _src,
                       {"en": [_nm.lower().split(" (")[0]]}, e_number=_en,
                       najis=True, evidence=["reformulation"]))


# ── 인니어(Bahasa) 별칭 주입 — 인도네시아 라벨/서류 매칭 ──
_ID_ALIASES = {
    "ing.salt": ["garam"], "ing.sugar": ["gula"], "ing.water": ["air"],
    "ing.lecithin": ["lesitin"], "ing.glycerin": ["gliserin"], "ing.citric_acid": ["asam sitrat"],
    "ing.ethanol_beverage": ["alkohol", "arak", "minuman keras"], "ing.gelatin": ["gelatin"],
    "ing.msg": ["vetsin", "penyedap"], "ing.lactic_acid": ["asam laktat"],
    "ing.lard": ["lemak babi", "minyak babi"], "ing.flavor": ["perisa", "pewangi"],
    "ing.enzyme": ["enzim"], "ing.carmine": ["karmin"], "ing.collagen": ["kolagen"],
    "ing.shortening": ["mentega putih"], "ing.tallow": ["lemak sapi"],
}
_by_uid = {r["ingredient_uid"]: r for r in ONTOLOGY}
for _uid, _al in _ID_ALIASES.items():
    if _uid in _by_uid:
        _amap = _by_uid[_uid].setdefault("aliases", {})
        _amap.setdefault("id", [])
        for _a in _al:
            if _a not in _amap["id"]:
                _amap["id"].append(_a)


def _load_ontology():
    """정본 데이터셋 로드 — ontology_data.json(310+종, 코드 버전관리) 우선, 없으면 ONTOLOGY(43 fallback)."""
    import os
    import json as _json
    p = os.path.join(os.path.dirname(__file__), "ontology_data.json")
    if os.path.exists(p):
        try:
            return _json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return ONTOLOGY


_COLS = {"ingredient_uid", "canonical_name", "category", "e_number", "default_status",
         "severity", "najis_risk", "carrier_check", "sources", "aliases",
         "required_evidence", "alternatives", "rule_version"}


def seed(db):
    """규칙버전 + 성분 온톨로지 증분 시딩 — uid 미존재분만 추가(기존 DB 보존, 대량확장 반영)."""
    if not db.get(RuleVersion, RULE_VERSION):
        db.add(RuleVersion(code=RULE_VERSION, jurisdiction="ID",
                           effective_from="2026-01-01", status="active"))
    existing = {u for (u,) in db.query(IngredientOntology.ingredient_uid).all()}
    added = 0
    for row in _load_ontology():
        uid = row.get("ingredient_uid")
        if not uid or uid in existing:
            continue
        db.add(IngredientOntology(**{k: v for k, v in row.items() if k in _COLS}))
        existing.add(uid)
        added += 1
    db.commit()
    return added