"""利用变量间关系的复核分析。

在逐字段范围核对之外提供三级证据：单元格可疑、整记录偏离、批次整体偏移
（参照批次可能不再适用）。方法全部先验固定，任何阈值都不接触待检批次：

- 参照批次先叠加人工复核意见（排除/修订只影响拟合，不改写原始记录）；
- 字段用中位数与 MAD 标准化，相关用 Gnanadesikan-Kettenring 稳健估计，
  参照批次自身混入少量错误时拟合仍然稳定；
- 稳健相关 |r|≥0.995 的近似重复列合并为代表列，相关矩阵做最近半正定修正；
- 每条记录在其实际观测到的字段子空间内计算稳健马氏距离，缺测不补零；
- 行级阈值是卡方 0.99 分位（先验），参照留一法距离只用于经验 p、
  参照自检与批次漂移自助检验；共同观测不足的字段对按独立处理并明确列出。
"""
from math import exp, lgamma, log, sqrt
from random import Random
from statistics import median

from .storage import fingerprint

NORM_CONSISTENCY = 1.4826  # 使 MAD 在正态下一致估计标准差
MIN_FIELD_OBSERVED = 8  # 字段进入模型所需的参照观测数
MIN_PAIR_COMMON = 12  # 估计两字段关系所需的共同观测数
DUPLICATE_CORRELATION = 0.995  # 近似重复列的稳健相关阈值
CELL_RESIDUAL_LIMIT = 3.0  # 单元格标准化条件残差限（先验）
FLAG_PROBABILITY = 0.99  # 行级卡方分位（先验）
MIN_CALIBRATION_RECORDS = 30  # 参照少于此数时声明校准不可靠
LOO_CAP = 300  # 参照多于此数时改用整体拟合距离并如实说明
BOOTSTRAP_ROUNDS = 2000
BOOTSTRAP_SIZE_CAP = 2000
DRIFT_P_VALUE = 0.01
BOOTSTRAP_SEED = 20261114  # 固定种子，同一数据版本结果可复现

_CHI2_CACHE = {}


def _scale(values):
    center = median(values)
    return center, NORM_CONSISTENCY * median(abs(value - center) for value in values)


def _quantile(values, probability):
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return ordered[low] * (1 - fraction) + ordered[high] * fraction


def _gamma_p(a, x):
    """正则化下不完全伽马函数 P(a, x)：级数与连分式实现。"""
    if x <= 0.0:
        return 0.0
    gln = lgamma(a)
    if x < a + 1.0:
        ap = a
        total = 1.0 / a
        delta = total
        for _ in range(1000):
            ap += 1.0
            delta *= x / ap
            total += delta
            if abs(delta) <= abs(total) * 1e-14:
                break
        return min(max(total * exp(-x + a * log(x) - gln), 0.0), 1.0)
    b = x + 1.0 - a
    c = 1.0 / 1e-300
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < 1e-300:
            d = 1e-300
        c = b + an / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-14:
            break
    return min(max(1.0 - exp(-x + a * log(x) - gln) * h, 0.0), 1.0)


def _chi2_threshold(df):
    """卡方分布 FLAG_PROBABILITY 分位，二分反解，结果缓存。"""
    if df in _CHI2_CACHE:
        return _CHI2_CACHE[df]
    low, high = 0.0, 1.0
    while _gamma_p(df / 2.0, high / 2.0) < FLAG_PROBABILITY:
        high *= 2.0
    for _ in range(80):
        middle = (low + high) / 2.0
        if _gamma_p(df / 2.0, middle / 2.0) < FLAG_PROBABILITY:
            low = middle
        else:
            high = middle
    _CHI2_CACHE[df] = (low + high) / 2.0
    return _CHI2_CACHE[df]


def _invert(matrix):
    """高斯-若尔当求逆（部分主元），奇异时逐级加岭重试。"""
    size = len(matrix)
    for ridge in (0.0, 1e-9, 1e-6, 1e-3):
        aug = [[matrix[i][j] + (ridge if i == j else 0.0) for j in range(size)]
               + [1.0 if i == j else 0.0 for j in range(size)] for i in range(size)]
        for col in range(size):
            pivot = max(range(col, size), key=lambda row: abs(aug[row][col]))
            if abs(aug[pivot][col]) < 1e-12:
                break
            aug[col], aug[pivot] = aug[pivot], aug[col]
            factor = aug[col][col]
            aug[col] = [value / factor for value in aug[col]]
            for row in range(size):
                if row != col and aug[row][col] != 0.0:
                    weight = aug[row][col]
                    aug[row] = [value - weight * other for value, other in zip(aug[row], aug[col])]
        else:
            return [row[size:] for row in aug]
    return None


def _jacobi(matrix):
    """循环雅可比特征分解，返回特征值与特征向量（列）。"""
    size = len(matrix)
    a = [list(row) for row in matrix]
    vectors = [[1.0 if i == j else 0.0 for j in range(size)] for i in range(size)]
    for _ in range(100):
        p, q, off = 0, 0, 0.0
        for i in range(size):
            for j in range(i + 1, size):
                if abs(a[i][j]) > off:
                    off, p, q = abs(a[i][j]), i, j
        if off < 1e-12:
            break
        theta = (a[q][q] - a[p][p]) / (2.0 * a[p][q])
        t = (1.0 if theta >= 0 else -1.0) / (abs(theta) + sqrt(theta * theta + 1.0))
        c = 1.0 / sqrt(t * t + 1.0)
        s = t * c
        for k in range(size):
            akp, akq = a[k][p], a[k][q]
            a[k][p] = c * akp - s * akq
            a[k][q] = s * akp + c * akq
        for k in range(size):
            apk, aqk = a[p][k], a[q][k]
            a[p][k] = c * apk - s * aqk
            a[q][k] = s * apk + c * aqk
        for k in range(size):
            vkp, vkq = vectors[k][p], vectors[k][q]
            vectors[k][p] = c * vkp - s * vkq
            vectors[k][q] = s * vkp + c * vkq
    return [a[i][i] for i in range(size)], vectors


def _nearest_psd(matrix):
    """特征值截断到 1e-8 后重建，并重新归一为单位对角。"""
    size = len(matrix)
    if size == 1:
        return [[1.0]]
    values, vectors = _jacobi(matrix)
    clipped = [max(value, 1e-8) for value in values]
    out = [[sum(vectors[i][k] * clipped[k] * vectors[j][k] for k in range(size))
            for j in range(size)] for i in range(size)]
    for i in range(size):
        for j in range(size):
            out[i][j] /= sqrt(out[i][i] * out[j][j])
    return out


def _robust_correlation(xs, ys):
    """Gnanadesikan-Kettenring 稳健相关；共同观测上无变异时返回 None。"""
    center_x, scale_x = _scale(xs)
    center_y, scale_y = _scale(ys)
    if scale_x <= 0 or scale_y <= 0:
        return None
    plus = [(x - center_x) / scale_x + (y - center_y) / scale_y for x, y in zip(xs, ys)]
    minus = [(x - center_x) / scale_x - (y - center_y) / scale_y for x, y in zip(xs, ys)]
    _, scale_plus = _scale(plus)
    _, scale_minus = _scale(minus)
    denominator = scale_plus ** 2 + scale_minus ** 2
    if denominator == 0:
        return None
    return max(-1.0, min(1.0, (scale_plus ** 2 - scale_minus ** 2) / denominator))


def _review_overlay(reviews, reference_ids):
    """把人工复核意见折算为拟合叠加层：整条排除、单格按缺测、单格修订。

    只影响参照拟合与试算，不回写原始记录；作用于非参照记录的意见不改变
    拟合，不计入生效列表。返回实际生效的意见编号。
    """
    excluded = set()
    overrides = {}
    applied = []
    for review in reviews:
        record_id = review.get("record_id")
        if record_id not in reference_ids:
            continue
        field = review.get("field")
        decision = review.get("decision")
        revised = review.get("revised_value")
        if decision == "exclude" and field is None:
            excluded.add(record_id)
            applied.append(review.get("review_id"))
        elif decision == "exclude" and field is not None:
            overrides.setdefault(record_id, {})[field] = None
            applied.append(review.get("review_id"))
        elif revised is not None and field is not None:
            overrides.setdefault(record_id, {})[field] = revised
            applied.append(review.get("review_id"))
    return excluded, overrides, applied


def _effective_values(record, override):
    values = {key: value for key, value in record["values"].items() if value is not None}
    for field, value in (override or {}).items():
        if value is None:
            values.pop(field, None)
        else:
            values[field] = value
    return values


def _fit(rows, field_ids):
    """在参照行上拟合尺度、稳健相关、近似重复分组与半正定相关矩阵。"""
    stats = {}
    dropped = []
    for field in field_ids:
        observed = [values[field] for _, values in rows if field in values]
        if len(observed) < MIN_FIELD_OBSERVED:
            dropped.append({"field": field, "reason": f"参照观测不足 {MIN_FIELD_OBSERVED} 条"})
            continue
        center, scale = _scale(observed)
        if scale <= 0:
            dropped.append({"field": field, "reason": "参照观测无变异"})
            continue
        stats[field] = {"center": center, "scale": scale, "observed": len(observed)}
    used = [field for field in field_ids if field in stats]
    correlations = {}
    insufficient = []
    for i, field in enumerate(used):
        for other in used[i + 1:]:
            pairs = [(values[field], values[other]) for _, values in rows
                     if field in values and other in values]
            correlation = None
            if len(pairs) >= MIN_PAIR_COMMON:
                correlation = _robust_correlation([x for x, _ in pairs], [y for _, y in pairs])
            if correlation is None:
                insufficient.append({"fields": [field, other], "common_observations": len(pairs)})
            else:
                correlations[frozenset((field, other))] = correlation
    parent = {field: field for field in used}

    def find(field):
        while parent[field] != field:
            parent[field] = parent[parent[field]]
            field = parent[field]
        return field

    for pair, correlation in correlations.items():
        if abs(correlation) >= DUPLICATE_CORRELATION:
            first, second = tuple(pair)
            root_first, root_second = find(first), find(second)
            if root_first != root_second:
                parent[root_second] = root_first
    groups = {}
    for field in used:
        groups.setdefault(find(field), []).append(field)
    representatives = []
    duplicate_groups = []
    for members in groups.values():
        members.sort(key=lambda field: (-stats[field]["observed"], field_ids.index(field)))
        representatives.append(members[0])
        if len(members) > 1:
            strongest = max(abs(correlations.get(frozenset((a, b)), 0.0))
                            for a in members for b in members if a != b)
            duplicate_groups.append({"kept": members[0], "merged": members[1:],
                                     "correlation": round(strongest, 4)})
    representatives.sort(key=field_ids.index)
    size = len(representatives)
    matrix = [[1.0 if i == j else correlations.get(frozenset((representatives[i], representatives[j])), 0.0)
               for j in range(size)] for i in range(size)]
    if size > 1:
        matrix = _nearest_psd(matrix)
    return {"fields": representatives, "stats": stats, "matrix": matrix,
            "sufficient_pairs": set(correlations), "duplicate_groups": duplicate_groups,
            "fields_dropped": dropped, "pairs_insufficient": insufficient}


def _score(model, values, units):
    """在观测到的模型字段子空间内评分：稳健距离 + 逐格条件残差。"""
    fields = model["fields"]
    observed = [field for field in fields if field in values]
    if not observed:
        return None
    stats = model["stats"]
    z = {field: (values[field] - stats[field]["center"]) / stats[field]["scale"] for field in observed}
    positions = [fields.index(field) for field in observed]
    sub = [[model["matrix"][i][j] for j in positions] for i in positions]
    inverse = _invert(sub)
    if inverse is None:
        return None
    d2 = 0.0
    for a, field_a in enumerate(observed):
        for b, field_b in enumerate(observed):
            d2 += z[field_a] * inverse[a][b] * z[field_b]
    d2 = max(d2, 0.0)
    cells = []
    for a, field in enumerate(observed):
        center, scale = stats[field]["center"], stats[field]["scale"]
        rest = [b for b in range(len(observed))
                if b != a and frozenset((field, observed[b])) in model["sufficient_pairs"]]
        if not rest:
            cells.append({"field": field, "value": values[field], "unit": units.get(field, ""),
                          "predictors": [], "note": "共同观测不足，无关联字段可核验"})
            continue
        sub_rest = [[sub[b][c] for c in rest] for b in rest]
        inverse_rest = _invert(sub_rest)
        if inverse_rest is None:
            continue
        rvec = [sub[b][a] for b in rest]
        coef = [sum(inverse_rest[i][j] * rvec[j] for j in range(len(rest))) for i in range(len(rest))]
        predicted_z = sum(coef[i] * z[observed[rest[i]]] for i in range(len(rest)))
        conditional_variance = max(1.0 - sum(rvec[i] * coef[i] for i in range(len(rest))), 1e-6)
        residual = (z[field] - predicted_z) / sqrt(conditional_variance)
        expected = center + predicted_z * scale
        half_width = CELL_RESIDUAL_LIMIT * scale * sqrt(conditional_variance)
        cells.append({"field": field, "value": values[field], "unit": units.get(field, ""),
                      "expected": round(expected, 4), "lower": round(expected - half_width, 4),
                      "upper": round(expected + half_width, 4), "residual": round(residual, 2),
                      "suspicious": abs(residual) > CELL_RESIDUAL_LIMIT,
                      "predictors": [observed[b] for b in rest]})
    return {"distance": sqrt(d2), "df": len(observed), "z": z, "cells": cells}


def _explain(status, distance, threshold_d2, p_value, cells):
    if status == "无法评估":
        return "与模型字段没有共同观测，无法评估；这不代表记录正常，只是无法判断。"
    parts = [f"稳健距离 {distance:.2f}（阈值 {sqrt(threshold_d2):.2f}，经验 p={p_value:.4f}）"]
    suspicious = [cell for cell in cells if cell.get("suspicious")]
    for cell in suspicious:
        parts.append(f"{cell['field']} 实测 {cell['value']} {cell['unit']}，"
                     f"按 {'、'.join(cell['predictors'])} 的关联预期约 {cell['expected']} {cell['unit']}"
                     f"（允许带 [{cell['lower']}, {cell['upper']}]），标准化残差 {cell['residual']}")
    if status == "整记录偏离" and not suspicious:
        parts.append("各单元格均在关联允许带内，偏离来自字段组合而非单一字段")
    if status == "一致":
        parts.append("各字段与参照的变量间关系一致")
    return "；".join(parts)


def assess(data, selection, extra_reviews=(), score_overrides=None):
    """对所选批次做三级复核；extra_reviews 与 score_overrides 仅用于试算。"""
    batch = selection.get("batch", "current")
    field_ids = [field["id"] for field in data["fields"]]
    units = {field["id"]: field.get("unit", "") for field in data["fields"]}
    review_lookup = {}
    for review in data.get("reviews", []):
        review_lookup.setdefault(review["record_id"], []).append(
            {"review_id": review["review_id"], "field": review["field"],
             "decision": review["decision"], "note": review["note"],
             "revised_value": review["revised_value"]})
    excluded, overrides, applied = _review_overlay(
        list(data.get("reviews", [])) + list(extra_reviews),
        {record["record_id"] for record in data["records"] if record["batch"] == "reference"})
    selected = [record for record in data["records"] if record["batch"] == batch]
    if not selected:
        raise ValueError("此批次没有测量记录")
    fit_rows = []
    originals = {}
    excluded_reference = []
    for record in data["records"]:
        if record["batch"] != "reference":
            continue
        record_id = record["record_id"]
        if record_id in excluded:
            excluded_reference.append(record_id)
            continue
        fit_rows.append((record_id, _effective_values(record, overrides.get(record_id))))
        originals[record_id] = {key: value for key, value in record["values"].items() if value is not None}
    limitations = []
    notes = ["行级阈值是先验卡方 0.99 分位，经验参考来自参照批次留一法；全程不使用待检批次校准。",
             "标记只提示需要人工复核：不删除、不修改原始记录，也不等同于造假结论。"]

    def degraded(reason):
        limitations.append(reason)
        records_view = [{"record_id": record["record_id"], "status": "无法评估",
                         "distance": None, "p": None, "df": 0,
                         "observed_fields": sum(1 for value in record["values"].values() if value is not None),
                         "missing_model_fields": [], "cells": [],
                         "reviews": review_lookup.get(record["record_id"], []),
                         "explanation": reason + "；这不代表记录正常，只是无法判断。"}
                        for record in selected]
        return {"summary_extra": {"需复核记录": 0, "批次判定": "无法判定",
                                  "参照拟合记录": len(fit_rows), "模型字段": 0},
                "records": records_view,
                "batch_assessment": {"batch": batch, "assessable": 0, "not_assessable": len(selected),
                                     "flagged": 0, "flag_fraction": None,
                                     "expected_flag_fraction": round(1 - FLAG_PROBABILITY, 4),
                                     "median_distance": None, "verdict": "无法判定", "drift_p": None,
                                     "explanation": reason, "field_shifts": []},
                "model": None, "limitations": limitations, "notes_extra": notes,
                "series": [], "threshold_line": None}

    if len(fit_rows) < MIN_FIELD_OBSERVED:
        return degraded(f"参照批次可用记录不足 {MIN_FIELD_OBSERVED} 条（当前 {len(fit_rows)} 条），"
                        "无法建立变量间关系模型")
    model = _fit(fit_rows, field_ids)
    if not model["fields"]:
        return degraded("参照批次没有任何字段满足建模条件（观测不足或无变异）")

    if len(fit_rows) <= LOO_CAP:
        calibration_method = f"留一法（{len(fit_rows)} 条参照）"
        pool = {}
        for index, (record_id, _) in enumerate(fit_rows):
            fold_model = _fit(fit_rows[:index] + fit_rows[index + 1:], field_ids)
            scored = _score(fold_model, originals[record_id], units)
            if scored is not None:
                pool[record_id] = (scored["distance"], scored["df"])
    else:
        calibration_method = f"整体拟合（参照 {len(fit_rows)} 条超过 {LOO_CAP} 条，未逐条留出）"
        pool = {}
        for record_id, _ in fit_rows:
            scored = _score(model, originals[record_id], units)
            if scored is not None:
                pool[record_id] = (scored["distance"], scored["df"])
    pool_distances = [distance for distance, _ in pool.values()]
    empirical_threshold = _quantile(pool_distances, FLAG_PROBABILITY)
    reliable = len(fit_rows) >= MIN_CALIBRATION_RECORDS
    score_overrides = score_overrides or {}

    records_view = []
    batch_distances = []
    field_z = {field: [] for field in model["fields"]}
    df_counts = {}
    flagged = 0
    not_assessable = 0
    for record in selected:
        record_id = record["record_id"]
        values = _effective_values(record, score_overrides.get(record_id))
        scored = _score(model, values, units)
        reviews = review_lookup.get(record_id, [])
        if scored is None:
            not_assessable += 1
            records_view.append({"record_id": record_id, "status": "无法评估",
                                 "distance": None, "p": None, "df": 0,
                                 "observed_fields": sum(1 for value in values),
                                 "missing_model_fields": list(model["fields"]),
                                 "cells": [], "reviews": reviews,
                                 "explanation": _explain("无法评估", None, None, None, [])})
            continue
        if batch == "reference" and record_id in pool and record_id not in score_overrides:
            distance, df = pool[record_id]
        else:
            distance, df = scored["distance"], scored["df"]
        threshold_d2 = _chi2_threshold(df)
        p_value = (1 + sum(1 for pooled in pool_distances if pooled >= distance)) / (len(pool_distances) + 1)
        suspicious_cells = [cell for cell in scored["cells"] if cell.get("suspicious")]
        if suspicious_cells:
            status = "单元格可疑"
        elif distance * distance > threshold_d2:
            status = "整记录偏离"
        else:
            status = "一致"
        if status != "一致":
            flagged += 1
        batch_distances.append(distance)
        df_counts[df] = df_counts.get(df, 0) + 1
        for field, z_value in scored["z"].items():
            field_z[field].append(z_value)
        notable = list(suspicious_cells)
        for cell in scored["cells"]:
            if cell.get("note") and cell not in notable:
                notable.append(cell)
        if not notable and status != "一致" and scored["cells"]:
            notable.append(max(scored["cells"], key=lambda cell: abs(cell.get("residual", 0.0))))
        records_view.append({
            "record_id": record_id, "status": status, "distance": round(distance, 3),
            "p": round(p_value, 4), "df": df, "observed_fields": len(values),
            "missing_model_fields": [field for field in model["fields"] if field not in values],
            "cells": notable[:6], "reviews": reviews,
            "explanation": _explain(status, distance, threshold_d2, p_value, notable[:6])})

    for pair in model["pairs_insufficient"]:
        limitations.append(f"字段对 {'×'.join(pair['fields'])} 共同观测仅 {pair['common_observations']} 条"
                           f"（不足 {MIN_PAIR_COMMON} 条），按相互独立处理；该对的关系异常无法识别")
    for dropped in model["fields_dropped"]:
        limitations.append(f"字段 {dropped['field']} 未进入模型：{dropped['reason']}")
    for group in model["duplicate_groups"]:
        limitations.append(f"近似重复列 {'、'.join(group['merged'])} 与 {group['kept']} 的稳健相关为 "
                           f"{group['correlation']}，建模保留 {group['kept']}，其余仅作展示")
    if not reliable:
        limitations.append(f"参照拟合记录 {len(fit_rows)} 条（少于 {MIN_CALIBRATION_RECORDS} 条），"
                           "经验阈值与漂移检验的不确定度高，结论仅作参考")
    if not_assessable:
        limitations.append(f"本批次 {not_assessable} 条记录与模型字段没有共同观测，标记为无法评估")

    modal_df = max(df_counts, key=df_counts.get) if df_counts else len(model["fields"])
    theoretical_distance = sqrt(_chi2_threshold(modal_df))
    if empirical_threshold is not None and empirical_threshold > 1.5 * theoretical_distance:
        limitations.append(f"参照留一法经验上限 {empirical_threshold:.2f} 明显高于理论阈值 "
                           f"{theoretical_distance:.2f}：参照批次可能混入未标记异常，建议先复核参照内极端记录")

    if batch == "reference":
        verdict = "参照批次自身"
        drift_p = None
        drift_explanation = "参照批次不做自身漂移检验；列出的记录距离为留一法结果。"
    elif not batch_distances or not pool_distances:
        verdict = "无法判定"
        drift_p = None
        drift_explanation = ("该批次没有可评估记录，无法做批次级判断。" if not batch_distances
                             else "参照批次没有可用的校准距离，无法做批次级判断。")
    else:
        sample = batch_distances
        if len(sample) > BOOTSTRAP_SIZE_CAP:
            rng_cap = Random(BOOTSTRAP_SEED)
            sample = rng_cap.sample(batch_distances, BOOTSTRAP_SIZE_CAP)
        observed_median = median(sample)
        rng = Random(BOOTSTRAP_SEED)
        exceed = 0
        for _ in range(BOOTSTRAP_ROUNDS):
            if median(rng.choice(pool_distances) for _ in range(len(sample))) >= observed_median:
                exceed += 1
        drift_p = (1 + exceed) / (BOOTSTRAP_ROUNDS + 1)
        verdict = "批次整体偏移" if drift_p <= DRIFT_P_VALUE else "与参照一致"
        drift_explanation = (f"批次稳健距离中位数 {observed_median:.2f}，参照留一法距离自助检验 "
                             f"p={drift_p:.4f}。")
    field_shifts = []
    for field in model["fields"]:
        if len(field_z[field]) >= 3:
            median_z = median(field_z[field])
            if abs(median_z) >= 2:
                field_shifts.append({"field": field, "median_z": round(median_z, 2),
                                     "direction": "偏高" if median_z > 0 else "偏低"})
    field_shifts.sort(key=lambda shift: -abs(shift["median_z"]))
    if verdict == "批次整体偏移":
        if field_shifts:
            drift_explanation += ("整体偏移字段：" + "、".join(
                f"{shift['field']}（中位{shift['direction']} {abs(shift['median_z']):.1f} 个稳健尺度）"
                for shift in field_shifts) + "。")
        drift_explanation += ("这更像批次条件变化或参照批次不再适用，而不是大量记录各自出错；"
                              "逐记录标记请在此背景下解读，必要时更新参照批次。")
    notes.append("批次判定：" + verdict + "。" + drift_explanation)

    extreme = sorted(pool.items(), key=lambda item: -item[1][0])[:3]
    model_block = {
        "fields_used": list(model["fields"]),
        "duplicate_groups": model["duplicate_groups"],
        "fields_dropped": model["fields_dropped"],
        "pairs_insufficient": model["pairs_insufficient"],
        "reference_fitted": len(fit_rows),
        "reference_excluded": sorted(excluded_reference),
        "reviews_applied": sorted({review_id for review_id in applied if review_id}),
        "calibration": {"method": calibration_method, "pool": len(pool_distances), "reliable": reliable},
        "flag_probability": FLAG_PROBABILITY,
        "cell_residual_limit": CELL_RESIDUAL_LIMIT,
        "empirical_threshold": round(empirical_threshold, 3) if empirical_threshold is not None else None,
        "correlation_matrix": {"fields": list(model["fields"]),
                               "matrix": [[round(value, 3) for value in row] for row in model["matrix"]]},
        "reference_extreme": [{"record_id": record_id, "distance": round(distance, 2)}
                              for record_id, (distance, _) in extreme],
        "fingerprint": fingerprint({
            "fields": model["fields"],
            "stats": {field: [model["stats"][field]["center"], model["stats"][field]["scale"]]
                      for field in model["fields"]},
            "matrix": model["matrix"],
            "pool": sorted(pool_distances),
            "reviews_applied": sorted(review_id for review_id in applied if review_id),
            "excluded_reference": sorted(excluded_reference),
            "constants": [MIN_FIELD_OBSERVED, MIN_PAIR_COMMON, DUPLICATE_CORRELATION,
                          CELL_RESIDUAL_LIMIT, FLAG_PROBABILITY]})}
    median_distance = median(batch_distances) if batch_distances else None
    return {"summary_extra": {"需复核记录": flagged, "批次判定": verdict,
                              "参照拟合记录": len(fit_rows), "模型字段": len(model["fields"])},
            "records": records_view,
            "batch_assessment": {"batch": batch, "assessable": len(batch_distances),
                                 "not_assessable": not_assessable, "flagged": flagged,
                                 "flag_fraction": round(flagged / len(selected), 4) if selected else None,
                                 "expected_flag_fraction": round(1 - FLAG_PROBABILITY, 4),
                                 "median_distance": round(median_distance, 3) if median_distance is not None else None,
                                 "verdict": verdict, "drift_p": round(drift_p, 4) if drift_p is not None else None,
                                 "explanation": drift_explanation, "field_shifts": field_shifts},
            "model": model_block, "limitations": limitations, "notes_extra": notes,
            "series": [{"label": entry["record_id"], "x": index + 1, "y": entry["distance"]}
                       for index, entry in enumerate(records_view) if entry["distance"] is not None],
            "threshold_line": round(theoretical_distance, 3)}
