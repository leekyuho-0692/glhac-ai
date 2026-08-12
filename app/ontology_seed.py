"""임계원재료 ontology seed — 설계 24.13/24.13.8 (illustrative, 전문가 검수 전).
carrier_check: 'alcohol'(향료 등 알코올 용매 점검) | 'gelatin'(카로틴 등 젤라틴 캐리어 점검)."""
from .models import IngredientOntology, RuleVersion

# 내부 초안 룰셋 — BPJPH/MUI 공식 고시를 그대로 옮긴 것이 아니라 전문가 검수 전 예시다.
# 공식 규정처럼 보이는 코드명을 쓰면 심사·대외 자료에서 근거를 오인하게 되므로 DRAFT로 명시한다.
RULE_VERSION = "GLHAC-DRAFT-2026-01"


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
       {"id": ["perisa", "pewangi"], "ko": ["향료", "착향료"],
        # 'taste powder'는 향료의 실무 표기 변형 — 종전엔 미매칭이었다('taste' 단독은 과잉매칭 위험).
        "en": ["flavor", "flavour", "fragrance", "aroma", "taste powder", "flavored powder"]},
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
    # ── 아미노산·비타민·감미료 보강 (웹 교차검증 2026-08: BPJPH/MUI 실무기준) ──
    _e("ing.taurine", "Taurine", "additive", "mushbooh", "medium", ["animal", "synthetic"],
       {"id": ["taurin"], "ko": ["타우린"], "en": ["taurine"]},
       evidence=["source_declaration", "halal_cert"], alts=["synthetic taurine (declared)"]),
    _e("ing.l_carnitine", "L-Carnitine", "additive", "mushbooh", "medium", ["animal", "synthetic"],
       {"id": ["l-karnitin", "karnitin"], "ko": ["L-카르니틴", "카르니틴"],
        "en": ["l-carnitine", "carnitine", "levocarnitine"]},
       evidence=["source_declaration", "halal_cert"], alts=["fermentation-derived L-carnitine"]),
    _e("ing.vitamin_premix", "Vitamin premix / Vitamin B complex", "additive", "mushbooh", "medium", ["unknown"],
       {"id": ["premiks vitamin"], "ko": ["비타민 프리믹스", "비타민B컴플렉스", "비타민미네랄믹스"],
        "en": ["vitamin b complex", "vitamin premix", "vitamin mineral mix", "vitaminmineralmix", "vitamin mix"]},
       carrier="gelatin", evidence=["composition_breakdown", "source_declaration", "halal_cert"]),
    _e("ing.gardenia_color", "Gardenia color (yellow/blue/red)", "colorant", "mushbooh", "low", ["plant"],
       {"id": ["gardenia"], "ko": ["치자색소", "치자"],
        "en": ["gardenia", "gardenia yellow", "gardenia blue", "gardenia red", "genipin"]},
       evidence=["composition_breakdown", "source_declaration"]),
    _e("ing.stevia_enzymatic", "Enzymatically modified stevia", "additive", "mushbooh", "low", ["plant"],
       {"id": ["stevia enzim"], "ko": ["효소처리스테비아", "효소 처리 스테비아"],
        "en": ["enzymatically modified stevia", "enzyme modified stevia", "stevia"]},
       evidence=["source_declaration"], alts=["steviol glycoside (declared enzyme origin)"]),
    _e("ing.l_arginine", "L-Arginine", "additive", "halal", "low", ["plant", "ferment", "synthetic"],
       {"id": ["l-arginin"], "ko": ["L-아르기닌", "아르기닌"], "en": ["l-arginine", "arginine"]},
       evidence=["source_declaration"]),
    _e("ing.l_theanine", "L-Theanine", "additive", "halal", "low", ["plant", "synthetic"],
       {"id": ["l-teanin"], "ko": ["L-테아닌", "테아닌"], "en": ["l-theanine", "theanine"]}),
    _e("ing.nicotinamide", "Nicotinamide", "additive", "halal", "low", ["synthetic"],
       {"id": ["nikotinamida"], "ko": ["니코틴아미드", "니아신아미드"],
        "en": ["nicotinamide", "niacinamide"]}),
    _e("ing.dextrose", "Dextrose", "base", "halal", "low", ["plant"],
       {"id": ["dekstrosa"], "ko": ["덱스트로스", "포도당"],
        "en": ["dextrose", "dextrose anhydrate", "glucose"]},
       evidence=["source_declaration"]),
    # ── 하람 보강 (웹 교차검증 2026-08: BPJPH Kepka 57/2021, MUI) ──
    # 돼지·혈액·사체·비할랄도축·사람유래·khamr는 출처 확인 대상이 아니라 '항상 하람'이다.
    _e("ing.pork", "Pork (swine)", "animal_protein", "haram", "high", ["animal"],
       {"id": ["babi", "daging babi"], "ko": ["돼지고기", "돈육"],
        "en": ["pork", "swine", "pig meat", "bacon", "ham (pork)"]},
       najis=True, evidence=["reformulation"], alts=["beef (halal slaughtered)", "chicken (halal)"]),
    _e("ing.pork_derivative", "Pork-derived material", "animal_protein", "haram", "high", ["animal"],
       {"id": ["turunan babi"], "ko": ["돼지유래", "돈지"],
        "en": ["pork derivative", "porcine", "pork fat", "pig fat"]},
       najis=True, evidence=["reformulation"], alts=["plant-based equivalent"]),
    _e("ing.pork_pepsin", "Pepsin (porcine)", "enzyme", "haram", "high", ["animal"],
       {"id": ["pepsin babi"], "ko": ["돼지펩신"],
        "en": ["porcine pepsin", "pork pepsin", "pepsin (porcine)"]},
       najis=True, evidence=["reformulation"], alts=["microbial protease"]),
    _e("ing.pork_collagen", "Collagen (porcine)", "animal_protein", "haram", "high", ["animal"],
       {"id": ["kolagen babi"], "ko": ["돼지콜라겐"],
        "en": ["porcine collagen", "pork collagen"]},
       najis=True, evidence=["reformulation"], alts=["fish collagen"]),
    _e("ing.blood", "Blood", "animal_protein", "haram", "high", ["animal"],
       {"id": ["darah"], "ko": ["혈액", "혈액추출물"],
        "en": ["blood", "blood extract", "blood powder"]},
       najis=True, evidence=["reformulation"]),
    _e("ing.blood_plasma", "Blood plasma / hemoglobin", "animal_protein", "haram", "high", ["animal"],
       {"id": ["plasma darah"], "ko": ["혈장", "헤모글로빈"],
        "en": ["blood plasma", "plasma protein", "hemoglobin", "hydrolyzed hemoglobin",
               "globulin concentrate", "fibrinogen"]},
       najis=True, evidence=["reformulation"]),
    _e("ing.carrion", "Carrion (bangkai)", "animal_protein", "haram", "high", ["animal"],
       {"id": ["bangkai"], "ko": ["사체", "폐사축"],
        "en": ["carrion", "dead animal", "not slaughtered"]},
       najis=True, evidence=["reformulation"]),
    _e("ing.non_halal_slaughter", "Non-halal slaughtered meat", "animal_protein", "haram", "high", ["animal"],
       {"id": ["sembelihan tidak halal"], "ko": ["비할랄도축", "비할랄육"],
        "en": ["non-halal meat", "non halal slaughter", "conventional slaughter meat"]},
       evidence=["halal_slaughter_certificate"], alts=["halal slaughtered meat"]),
    _e("ing.human_derived", "Human-derived material", "animal_protein", "haram", "high", ["human"],
       {"id": ["rambut manusia", "plasenta manusia"], "ko": ["사람모발", "인태반", "인체유래"],
        "en": ["human hair", "human placenta", "human derived", "human keratin"]},
       najis=True, evidence=["reformulation"], alts=["synthetic or microbial source"]),
    _e("ing.khamr_derivative", "Khamr by-product", "alcohol", "haram", "high", ["ferment"],
       {"id": ["khamr", "turunan khamr"], "ko": ["주정부산물", "주류부산물"],
        "en": ["khamr", "wine derivative", "brewing by-product", "wine vinegar (khamr)"]},
       evidence=["reformulation"]),
    _e("ing.dog", "Dog-derived material", "animal_protein", "haram", "high", ["animal"],
       {"id": ["anjing"], "ko": ["개", "견육"], "en": ["dog", "canine"]},
       najis=True, evidence=["reformulation"]),
    # 에탄올 일반형은 하람이 아니다 — khamr 유래 여부와 최종 함량(0.5% 미만)에 달렸다.
    _e("ing.ethanol", "Ethanol (industrial/synthetic)", "alcohol", "mushbooh", "high",
       ["ferment", "synthetic"],
       {"id": ["etanol", "alkohol etil"], "ko": ["에탄올", "에틸알코올", "주정"],
        "en": ["ethanol", "ethyl alcohol", "alcohol"]},
       carrier="alcohol",
       evidence=["alcohol_carrier_check", "source_declaration", "residual_alcohol_test"],
       alts=["non-alcoholic solvent", "propylene glycol"]),
    # ── 커버리지 보강 ① 식물·광물성 기초원료 (2026-08) ──
    # 식물·광물 유래는 기본 할랄이나, 가공보조제·정제공정은 심사에서 확인 대상이라 evidence를 남긴다.
    _e("ing.rice", "Rice", "base", "halal", "low", ["plant"],
       {"id": ["beras", "nasi"], "ko": ["쌀", "쌀가루", "현미", "누룽지"],
        "en": ["rice", "rice powder", "brown rice", "roasted brown rice", "nurungji"]}),
    _e("ing.wheat", "Wheat", "base", "halal", "low", ["plant"],
       {"id": ["gandum", "tepung terigu"], "ko": ["밀", "밀가루", "통밀", "밀식이섬유"],
        "en": ["wheat", "wheat flour", "whole wheat", "wheat dietary fiber", "wheat fiber"]}),
    _e("ing.oat", "Oat", "base", "halal", "low", ["plant"],
       {"id": ["oat", "havermut"], "ko": ["귀리", "오트", "오트분말"],
        "en": ["oat", "oat powder", "oat fiber", "roasted oat"]}),
    _e("ing.rye", "Rye", "base", "halal", "low", ["plant"],
       {"id": ["gandum hitam"], "ko": ["호밀", "호밀가루"], "en": ["rye", "rye powder"]}),
    _e("ing.corn_starch", "Corn starch / syrup", "base", "halal", "low", ["plant"],
       {"id": ["pati jagung", "sirup jagung"], "ko": ["옥수수전분", "콘시럽", "물엿"],
        "en": ["corn starch", "cornstarch", "corn syrup", "glucose syrup"]},
       evidence=["source_declaration"]),
    _e("ing.malt_syrup", "Malt syrup", "base", "halal", "low", ["plant"],
       {"id": ["sirup malt"], "ko": ["맥아시럽", "조청"], "en": ["malt syrup", "malt extract"]},
       evidence=["alcohol_carrier_check"]),
    _e("ing.soybean", "Soybean / soy protein", "base", "halal", "low", ["plant"],
       {"id": ["kedelai", "protein kedelai"], "ko": ["대두", "콩", "분리대두단백", "볶은콩"],
        "en": ["soybean", "soy protein", "isolated soy protein", "roasted soybean", "black bean"]},
       evidence=["source_declaration"]),
    _e("ing.fruit_concentrate", "Fruit juice/concentrate powder", "base", "halal", "low", ["plant"],
       {"id": ["konsentrat buah", "bubuk buah"], "ko": ["과즙분말", "농축과즙", "과일농축분말"],
        "en": ["juice powder", "concentrate powder", "fruit concentrate", "blueberry concentrate",
               "green apple concentrate", "green grape concentrate", "lychee concentrate",
               "shine muscat", "lemon juice powder", "peach juice powder", "sweet potato concentrate"]},
       evidence=["alcohol_carrier_check"]),
    _e("ing.banana", "Banana", "base", "halal", "low", ["plant"],
       {"id": ["pisang"], "ko": ["바나나"], "en": ["banana", "banana chip"]}),
    _e("ing.sweet_potato", "Sweet potato", "base", "halal", "low", ["plant"],
       {"id": ["ubi jalar"], "ko": ["고구마", "고구마분말"],
        "en": ["sweet potato", "sweet potato powder", "sweet potato extract"]}),
    _e("ing.tea_extract", "Tea extract (green/black/matcha)", "base", "halal", "low", ["plant"],
       {"id": ["ekstrak teh", "teh hijau"], "ko": ["녹차", "홍차추출물", "말차", "차추출물"],
        "en": ["tea extract", "green tea", "black tea", "matcha", "tea powder"]},
       evidence=["alcohol_carrier_check"]),
    _e("ing.coffee", "Coffee", "base", "halal", "low", ["plant"],
       {"id": ["kopi"], "ko": ["커피", "커피분말"], "en": ["coffee", "coffee powder", "coffee 100%"]}),
    _e("ing.coconut", "Coconut (milk/oil/cream/sap)", "base", "halal", "low", ["plant"],
       {"id": ["kelapa", "santan", "minyak kelapa"], "ko": ["코코넛", "코코넛오일", "코코넛밀크", "코코넛꽃수액"],
        "en": ["coconut", "coconut milk", "coconut oil", "coconut cream", "coconut flower sap"]}),
    _e("ing.cocoa", "Cocoa (powder/mass/butter)", "base", "halal", "low", ["plant"],
       {"id": ["kakao", "bubuk kakao"], "ko": ["코코아", "코코아분말", "카카오매스", "코코아버터"],
        "en": ["cocoa", "cocoa powder", "cocoa mass", "cocoa butter", "cacao"]}),
    _e("ing.fructose", "Crystalline fructose", "base", "halal", "low", ["plant"],
       {"id": ["fruktosa"], "ko": ["결정과당", "과당"], "en": ["fructose", "crystalline fructose"]}),
    _e("ing.seaweed_calcium", "Seaweed calcium", "base", "halal", "low", ["plant", "mineral"],
       {"id": ["kalsium rumput laut"], "ko": ["해조칼슘"], "en": ["seaweed calcium", "algae calcium"]}),
    _e("ing.phosphate_salt", "Sodium metaphosphate / pyrophosphate", "additive", "halal", "low", ["mineral"],
       {"id": ["natrium metafosfat"], "ko": ["메타인산나트륨", "피로인산나트륨"],
        "en": ["sodium metaphosphate", "sodium pyrophosphate", "tetrasodium pyrophosphate"]},
       evidence=["source_declaration"]),
    _e("ing.zinc_oxide", "Zinc oxide", "additive", "halal", "low", ["mineral"],
       {"id": ["seng oksida"], "ko": ["산화아연"], "en": ["zinc oxide"]}),
    _e("ing.ferrous_salt", "Ferrous fumarate / iron salt", "additive", "halal", "low", ["mineral", "synthetic"],
       {"id": ["besi fumarat"], "ko": ["푸마르산제일철", "철분제"],
        "en": ["ferrous fumarate", "ferrous sulfate", "iron fumarate"]}),
    _e("ing.synthetic_color", "Synthetic food color (FD&C)", "colorant", "halal", "low", ["synthetic"],
       {"id": ["pewarna sintetis"], "ko": ["합성착색료", "적색40호"],
        "en": ["fd&c", "fd&c red", "allura red", "synthetic color"]},
       evidence=["source_declaration"]),
    # ── 커버리지 보강 ② 확인 대상 — 캐리어·출처 불명 (2026-08) ──
    # 이름만으로 할랄을 단정할 수 없는 것들. 지용성 비타민 제제는 젤라틴 캐리어,
    # '가공유'류는 원료 유지의 동물성 여부, 복합원료는 구성 명세가 쟁점이다.
    _e("ing.vitamin_fatsoluble", "Fat-soluble vitamin preparation (A/D/E)", "additive", "mushbooh", "medium",
       ["unknown"],
       {"id": ["vitamin larut lemak"], "ko": ["지용성비타민제제", "비타민A제제", "비타민E제제", "토코페롤아세테이트"],
        "en": ["vitamin a mixed preparations", "vitamin e mixed preparations",
               "vitamin a fatty acid ester", "tocopheryl acetate", "di-a-tocopheryl acetate",
               "retinyl", "vitamin d preparation"]},
       carrier="gelatin",
       evidence=["composition_breakdown", "source_declaration", "halal_cert"],
       alts=["fish gelatin coated", "starch-coated beadlet"]),
    _e("ing.vitamin_watersoluble", "Water-soluble vitamin (B/C group)", "additive", "halal", "low",
       ["synthetic"],
       {"id": ["vitamin larut air"],
        "ko": ["수용성비타민", "비타민C", "비타민B2", "티아민", "피리독신", "엽산", "판토텐산칼슘"],
        "en": ["vitamin c", "ascorbic acid", "vitamin b2", "riboflavin", "thiamine hydrochloride",
               "pyridoxine hydrochloride", "folic acid", "calcium pantothenate"]},
       evidence=["source_declaration"]),
    _e("ing.processed_oil", "Processed / refined oil (source unspecified)", "base", "mushbooh", "medium",
       ["unknown"],
       {"id": ["minyak olahan"], "ko": ["가공유지", "정제가공유", "가공유"],
        "en": ["processed oil", "refined processed oil", "refined oil", "edible oil (unspecified)"]},
       evidence=["source_declaration", "halal_cert"],
       alts=["declared vegetable oil"]),
    _e("ing.vegetable_oil", "Vegetable oil", "base", "halal", "low", ["plant"],
       {"id": ["minyak nabati", "minyak sawit"], "ko": ["식물성유지", "팜유", "경화팜유"],
        "en": ["vegetable oil", "palm oil", "hydrogenated palm oil", "canola oil", "sunflower oil"]},
       evidence=["source_declaration"]),
    _e("ing.vegetable_cream", "Vegetable cream / creamer", "base", "mushbooh", "low", ["plant", "unknown"],
       {"id": ["krimer nabati"], "ko": ["식물성크림", "크리머", "베지터블크림"],
        "en": ["vegetable cream", "non-dairy creamer", "creamer"]},
       evidence=["composition_breakdown", "source_declaration"]),
    _e("ing.milk_powder", "Milk powder", "base", "halal", "low", ["animal"],
       {"id": ["susu bubuk"], "ko": ["분유", "탈지분유", "전지분유", "혼합분유"],
        "en": ["milk powder", "skim milk powder", "whole milk powder", "skimmed milk",
               "mixed milk powder"]},
       evidence=["source_declaration"]),
    _e("ing.milk_protein", "Milk protein isolate / concentrate", "animal_protein", "mushbooh", "medium",
       ["animal"],
       {"id": ["protein susu"], "ko": ["유단백", "분리유단백", "우유단백질"],
        "en": ["milk protein", "milk protein isolate", "milk protein concentrate", "milk protein crisp"]},
       evidence=["halal_cert", "source_declaration"],
       alts=["microbial rennet declared"]),
    _e("ing.chocolate", "Chocolate (milk/white/compound)", "base", "mushbooh", "low", ["plant", "animal"],
       {"id": ["cokelat", "cokelat susu"], "ko": ["초콜릿", "밀크초콜릿", "화이트초콜릿", "초콜릿분말"],
        "en": ["chocolate", "milk chocolate", "white chocolate", "chocolate powder",
               "compound chocolate"]},
       evidence=["composition_breakdown", "source_declaration"]),
    _e("ing.compound_premix", "Compound premix (composition unspecified)", "additive", "mushbooh", "medium",
       ["unknown"],
       {"id": ["campuran bahan tambahan", "formulasi campuran"],
        "ko": ["복합첨가물", "혼합제제", "혼합조제품"],
        "en": ["food additive mixture", "mixed formulation", "mixed preparation", "compound premix"]},
       evidence=["composition_breakdown", "source_declaration", "halal_cert"]),
    _e("ing.compound_snack", "Compound snack ball / crisp", "base", "mushbooh", "low", ["unknown"],
       {"id": ["bola sereal"], "ko": ["시리얼볼", "곡물볼", "크리스프"],
        "en": ["oat ball", "oat choco ball", "oat coco ball", "matcha oat ball",
               "protein ball", "crisp ball"]},
       evidence=["composition_breakdown"]),
    _e("ing.polishing_agent", "Polishing / glazing agent", "glazing", "mushbooh", "medium", ["unknown"],
       {"id": ["bahan pengkilap"], "ko": ["광택제", "표면처리제"],
        "en": ["polishing", "polishing agent", "glazing agent"]},
       evidence=["source_declaration", "halal_cert"],
       alts=["carnauba wax", "plant-based glaze"]),
    _e("ing.triacetin", "Triacetin (glyceryl triacetate)", "additive", "mushbooh", "medium",
       ["plant", "animal", "synthetic"],
       {"id": ["triasetin"], "ko": ["트리아세틴"], "en": ["triacetin", "glyceryl triacetate"]},
       e_number="E1518", carrier="alcohol",
       evidence=["source_declaration", "alcohol_carrier_check"]),
    # ── 성분 재검증 보정 (2026-08, 웹 교차검증) ──
    # E473이 'sugar' 별칭에 걸려 할랄로 통과하던 미탐을 막는다. 지방산 유래가 쟁점이라
    # E471·E570과 같은 기준(출처 의존)으로 둔다.
    _e("ing.e473", "Sucrose esters of fatty acids", "emulsifier", "mushbooh", "medium",
       ["plant", "animal"],
       {"id": ["ester sukrosa asam lemak"], "ko": ["자당지방산에스테르", "수크로스지방산에스테르"],
        "en": ["sucrose fatty acid ester", "sucrose esters of fatty acids", "sucrose ester"]},
       e_number="E473",
       evidence=["source_declaration", "halal_cert"],
       alts=["plant-derived fatty acid declared"]),
    # 해조(Lithothamnion) 유래 해양 미네랄 — 동물 유래 없음. 종전에는 'water'에 잘못 걸렸다.
    _e("ing.marine_mineral", "Marine mineral calcium (Aquamin/Lithothamnion)", "additive",
       "halal", "low", ["plant", "mineral"],
       {"id": ["kalsium laut"], "ko": ["해양미네랄", "해조칼슘복합", "아쿠아민"],
        "en": ["aquamin", "lithothamnion", "marine mineral", "aqua calcium", "marine calcium"]}),
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
                           effective_from="2026-01-01", status="draft"))
    # 과거에 심긴 공식 규정형 코드명은 active로 남겨두면 근거를 오인시킨다 → draft로 강등
    _legacy = db.get(RuleVersion, "BPJPH-2026-01")
    if _legacy is not None and _legacy.status != "draft":
        _legacy.status = "draft"
    existing = {u for (u,) in db.query(IngredientOntology.ingredient_uid).all()}
    added = 0
    for row in _load_ontology():
        uid = row.get("ingredient_uid")
        if not uid or uid in existing:
            continue
        vals = {k: v for k, v in row.items() if k in _COLS}
        # 데이터 파일에 옛 코드명이 박혀 있어도 코드 상수를 정본으로 삼는다(공식 규정 오인 방지)
        vals["rule_version"] = RULE_VERSION
        db.add(IngredientOntology(**vals))
        existing.add(uid)
        added += 1
    db.commit()
    return added