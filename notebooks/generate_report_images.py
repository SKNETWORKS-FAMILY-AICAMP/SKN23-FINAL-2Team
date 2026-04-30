"""
File    : notebooks/generate_report_images.py
Author  : 김다빈
Create  : 2026-04-24
Description :
    서류용 이미지 IMG-01 ~ IMG-10 생성 스크립트.
    dwg_to_classifier.ipynb 전체 실행 완료 후 마지막 셀로 붙여넣어 실행.

    exec(open("../notebooks/generate_report_images.py").read())

    필요 변수 (노트북에서 미리 정의되어야 함):
        X, y, X_train, X_test, y_train, y_test
        feature_names, le, cv_results, test_results, models
        best_model, y_cv_pred, per_class_f1, cv_macro_f1
        cv, N_FOLDS, json_records (선택)

Modification History :
    2026-04-24 | 김다빈 | 최초 작성
"""

import warnings
warnings.filterwarnings("ignore")

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import seaborn as sns
from pathlib import Path
from sklearn.metrics import confusion_matrix, roc_curve, auc
from sklearn.preprocessing import label_binarize
from sklearn.model_selection import cross_val_predict

# ── 출력 경로 ────────────────────────────────────────────────────────────────
_HERE = Path(globals().get("__file__", ".")).resolve()
OUT = _HERE.parent.parent / "docs" / "classifier" / "figures" / "final"
# exec로 실행 시 __file__ 없을 수 있으므로 절대경로 폴백
if not OUT.parent.exists():
    OUT = Path("/Users/gimdabin/SKN23-FINAL-2TEAM/docs/classifier/figures/final")
OUT.mkdir(parents=True, exist_ok=True)

# ── 공통 스타일 ──────────────────────────────────────────────────────────────
plt.rcParams.update({
    "font.family": "AppleGothic",
    "axes.unicode_minus": False,
    "figure.dpi": 150,
    "savefig.dpi": 150,
    "savefig.bbox": "tight",
    "axes.spines.top": False,
    "axes.spines.right": False,
})

DC = {"arch": "#2196F3", "elec": "#FF9800", "fire": "#F44336", "pipe": "#4CAF50"}
DKR = {"arch": "건축", "elec": "전기", "fire": "소방", "pipe": "배관"}

print("=" * 60)
print("  IMG-01 ~ IMG-10 생성 시작")
print(f"  저장 경로: {OUT}")
print("=" * 60)

# ────────────────────────────────────────────────────────────────────────────
# IMG-01 : 데이터셋 EDA
# ────────────────────────────────────────────────────────────────────────────
print("\n[IMG-01] 데이터셋 EDA 생성 중...")
try:
    domain_order = ["arch", "pipe", "fire", "elec"]
    domain_labels = [f"{DKR[d]}\n({d})" for d in domain_order]

    # json_records 또는 X/y에서 도메인별 수 집계
    try:
        counts = [sum(1 for r in json_records if r["domain"] == d) for d in domain_order]
        layer_data  = [[r["layer_count"]  for r in json_records if r["domain"] == d] for d in domain_order]
        entity_data = [[min(r["entity_count"], 30000) for r in json_records if r["domain"] == d] for d in domain_order]
        has_detail = True
    except NameError:
        # json_records 없으면 y 레이블에서 집계
        cls_list = list(le.classes_)
        counts = [int(np.sum(y == cls_list.index(d))) for d in domain_order]
        has_detail = False

    fig, axes = plt.subplots(1, 3 if has_detail else 1, figsize=(14 if has_detail else 6, 5))
    fig.suptitle(f"도메인별 데이터 분포 (총 {len(y)}개, 증강 포함)", fontsize=13, fontweight="bold")

    ax = axes[0] if has_detail else axes
    bars = ax.bar(domain_labels, counts,
                  color=[DC[d] for d in domain_order],
                  edgecolor="white", linewidth=1.5)
    for bar, cnt in zip(bars, counts):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + max(counts)*0.02,
                str(cnt), ha="center", va="bottom", fontsize=12, fontweight="bold")
    ax.set_title("도메인별 샘플 수 (증강 후)")
    ax.set_ylabel("샘플 수")
    ax.set_ylim(0, max(counts) * 1.2)

    if has_detail:
        for ax_sub, data, title, ylabel in [
            (axes[1], layer_data,  "레이어 수 분포",            "layer_count"),
            (axes[2], entity_data, "엔티티 수 분포 (30k 상한)", "entity_count"),
        ]:
            bp = ax_sub.boxplot(data, patch_artist=True,
                                medianprops=dict(color="white", linewidth=2))
            for patch, d in zip(bp["boxes"], domain_order):
                patch.set_facecolor(DC[d]); patch.set_alpha(0.8)
            ax_sub.set_xticklabels(domain_labels)
            ax_sub.set_title(title); ax_sub.set_ylabel(ylabel)

    plt.tight_layout()
    fig.savefig(OUT / "IMG-01_eda.png")
    plt.close(fig)
    print("  → IMG-01_eda.png 저장 완료")
except Exception as e:
    print(f"  → IMG-01 실패: {e}")

# ────────────────────────────────────────────────────────────────────────────
# IMG-02 : 피처 설계 단계별 성능 타임라인
# ────────────────────────────────────────────────────────────────────────────
print("[IMG-02] 피처 타임라인 생성 중...")
try:
    # 이전 실험 고정값 + 현재 노트북 결과
    stages = [
        "1단계\n38차원\n(실제 224개)\n합성 폐기 후",
        "2단계\n38차원\n(실제 435개)\n다출처 추가",
        "3단계\n58차원\n(435개)\nAIA 접두어",
        "4단계\n74차원\n(435개)\n키워드+TEXT",
        "최종\n85차원\n(1000개)\n증강+소방배관 강화",
    ]
    f1_history = [0.71, 0.67, 0.74, 0.807]
    # 현재 모델 CV F1
    current_f1 = cv_results["CatBoost"]["test_macro_f1"].mean()
    f1_values  = f1_history + [current_f1]
    colors = ["#B0BEC5", "#90A4AE", "#64B5F6", "#1E88E5", "#F44336"]

    fig, ax = plt.subplots(figsize=(13, 6))
    bars = ax.bar(stages, f1_values, color=colors, edgecolor="white",
                  linewidth=1.5, width=0.55, zorder=3)

    for bar, f1, is_final in zip(bars, f1_values, [False]*4 + [True]):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.01,
                f"{f1:.3f}", ha="center", va="bottom",
                fontsize=12 if is_final else 11,
                fontweight="bold" if is_final else "normal",
                color="#B71C1C" if is_final else "#333")

    ax.axhline(0.80, color="gray", ls="--", lw=1.5, zorder=2, label="목표 0.80")
    ax.axhline(0.95, color="#F44336", ls=":", lw=1.5, zorder=2, label="0.95 달성선")
    ax.set_ylim(0.45, 1.05)
    ax.set_ylabel("CV Macro F1", fontsize=12)
    ax.set_title("피처 설계 단계별 성능 개선 타임라인\n"
                 "(데이터 증강 + 85차원 피처로 CV F1 0.9567 달성)",
                 fontsize=12, fontweight="bold")
    ax.legend(fontsize=10)
    ax.grid(axis="y", alpha=0.4, zorder=1)
    plt.tight_layout()
    fig.savefig(OUT / "IMG-02_feature_timeline.png")
    plt.close(fig)
    print("  → IMG-02_feature_timeline.png 저장 완료")
except Exception as e:
    print(f"  → IMG-02 실패: {e}")

# ────────────────────────────────────────────────────────────────────────────
# IMG-03 : CV vs 테스트셋 Macro F1 비교
# ────────────────────────────────────────────────────────────────────────────
print("[IMG-03] CV vs 테스트셋 비교 생성 중...")
try:
    model_names = list(cv_results.keys())
    cv_f1s  = [cv_results[n]["test_macro_f1"].mean() for n in model_names]
    cv_stds = [cv_results[n]["test_macro_f1"].std()  for n in model_names]
    test_f1s = ([test_results[n]["macro_f1"] for n in model_names]
                if test_results else [])

    x = np.arange(len(model_names))
    w = 0.35
    fig, ax = plt.subplots(figsize=(12, 6))

    bars1 = ax.bar(x - w/2, cv_f1s, w, yerr=cv_stds, capsize=5,
                   label="CV Macro F1 (train 70%)",
                   color="#90CAF9", edgecolor="white",
                   error_kw=dict(lw=1.5, capthick=1.5))
    if test_f1s:
        bars2 = ax.bar(x + w/2, test_f1s, w,
                       label="Test Macro F1 (holdout 30%)",
                       color="#1565C0", edgecolor="white", alpha=0.9)
        for bar, v in zip(bars2, test_f1s):
            ax.text(bar.get_x() + bar.get_width()/2, v + 0.005,
                    f"{v:.3f}", ha="center", va="bottom", fontsize=8, fontweight="bold")

    for bar, v in zip(bars1, cv_f1s):
        ax.text(bar.get_x() + bar.get_width()/2, v + cv_stds[cv_f1s.index(v)] + 0.008,
                f"{v:.3f}", ha="center", va="bottom", fontsize=8)

    # CatBoost 강조
    if "CatBoost" in model_names:
        cb = model_names.index("CatBoost")
        for bars in ([bars1] + ([bars2] if test_f1s else [])):
            bars[cb].set_edgecolor("#C62828")
            bars[cb].set_linewidth(2.5)
        ax.annotate("★ 최종 선정\n(CatBoost)",
                    xy=(cb, cv_f1s[cb] + cv_stds[cb] + 0.01),
                    xytext=(cb + 0.7, cv_f1s[cb] + 0.04),
                    arrowprops=dict(arrowstyle="->", color="#C62828"),
                    fontsize=9, color="#C62828", fontweight="bold")

    ax.set_xticks(x); ax.set_xticklabels(model_names, rotation=10)
    ax.set_ylim(0.5, max(cv_f1s) * 1.15)
    ax.set_ylabel("Macro F1", fontsize=12)
    ax.set_title("모델별 CV vs 테스트셋 Macro F1 비교\n(CatBoost 최종 선정)",
                 fontsize=12, fontweight="bold")
    ax.legend(); ax.grid(axis="y", alpha=0.4)
    plt.tight_layout()
    fig.savefig(OUT / "IMG-03_cv_vs_test.png")
    plt.close(fig)
    print("  → IMG-03_cv_vs_test.png 저장 완료")
except Exception as e:
    print(f"  → IMG-03 실패: {e}")

# ────────────────────────────────────────────────────────────────────────────
# IMG-04 : CatBoost 혼동 행렬
# ────────────────────────────────────────────────────────────────────────────
print("[IMG-04] 혼동 행렬 생성 중...")
try:
    # 테스트셋 기준 시도, 없으면 CV 기준
    try:
        cb_model = models["CatBoost"]
        cb_model.fit(X_train, y_train)
        y_pred_t = cb_model.predict(X_test)
        cm_abs  = confusion_matrix(y_test, y_pred_t)
        subtitle = f"테스트셋 ({len(y_test)}개)"
    except Exception:
        cm_abs  = confusion_matrix(y, y_cv_pred)
        subtitle = "CV 기반 (전체 데이터)"

    cm_pct = cm_abs.astype(float) / cm_abs.sum(axis=1, keepdims=True)
    cls    = list(le.classes_)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.5))
    fig.suptitle(f"CatBoost — 혼동 행렬 ({subtitle})", fontsize=13, fontweight="bold")

    sns.heatmap(cm_abs, annot=True, fmt="d", cmap="Blues",
                xticklabels=cls, yticklabels=cls, ax=ax1,
                linewidths=0.5, linecolor="white")
    ax1.set_xlabel("예측"); ax1.set_ylabel("실제"); ax1.set_title("절댓값")

    annot = np.array([[f"{v:.2f}" for v in row] for row in cm_pct])
    sns.heatmap(cm_pct, annot=annot, fmt="", cmap="Blues",
                xticklabels=cls, yticklabels=cls, ax=ax2,
                vmin=0, vmax=1, linewidths=0.5, linecolor="white")
    ax2.set_xlabel("예측"); ax2.set_ylabel("실제"); ax2.set_title("비율 (행 합=1.0)")

    # 소방→배관 오분류 강조
    try:
        fi, pi = cls.index("fire"), cls.index("pipe")
        for ax_ in (ax1, ax2):
            ax_.add_patch(plt.Rectangle((pi, fi), 1, 1, fill=False,
                                        edgecolor="red", lw=2.5))
    except ValueError:
        pass

    plt.tight_layout()
    fig.savefig(OUT / "IMG-04_confusion_matrix_catboost.png")
    plt.close(fig)
    print("  → IMG-04_confusion_matrix_catboost.png 저장 완료")
except Exception as e:
    print(f"  → IMG-04 실패: {e}")

# ────────────────────────────────────────────────────────────────────────────
# IMG-05 : CatBoost 피처 중요도 Top 20
# ────────────────────────────────────────────────────────────────────────────
print("[IMG-05] 피처 중요도 생성 중...")
try:
    import pandas as pd
    clf_step = best_model.named_steps.get("clf")
    if not hasattr(clf_step, "feature_importances_"):
        raise ValueError("feature_importances_ 없음")

    fi_df = pd.DataFrame({
        "feature": feature_names,
        "importance": clf_step.feature_importances_,
    }).sort_values("importance", ascending=False).head(20)

    GROUP_COLOR = {
        "txt_": "#E91E63", "kw_": "#9C27B0", "aia_": "#FF9800",
        "ent_": "#2196F3", "log_": "#4CAF50", "unit": "#009688",
        "has_": "#607D8B",
    }
    def _gc(name):
        for p, c in GROUP_COLOR.items():
            if name.startswith(p): return c
        return "#90A4AE"

    colors = [_gc(f) for f in fi_df["feature"]]
    fig, ax = plt.subplots(figsize=(10, 8))
    ax.barh(fi_df["feature"][::-1], fi_df["importance"][::-1],
            color=colors[::-1], edgecolor="white", linewidth=0.8)
    mx = fi_df["importance"].max()
    for i, (_, row) in enumerate(fi_df[::-1].iterrows()):
        ax.text(row["importance"] + mx*0.01,
                i, f"{row['importance']:.4f}", va="center", fontsize=9)

    ax.set_xlabel("Feature Importance", fontsize=11)
    ax.set_title(f"CatBoost 피처 중요도 Top 20 (총 {len(feature_names)}차원)",
                 fontsize=12, fontweight="bold")
    patches = [mpatches.Patch(color=c, label=l) for c, l in [
        ("#E91E63","[P] TEXT 정규식"), ("#9C27B0","[O] 레이어 키워드"),
        ("#FF9800","[H] AIA 접두어"), ("#2196F3","[A] 엔티티 비율"),
        ("#4CAF50","[D] 수치통계"),   ("#009688","[E] drawing_unit"),
        ("#607D8B","[B] 이진 존재"),  ("#90A4AE","기타"),
    ]]
    ax.legend(handles=patches, loc="lower right", fontsize=8)
    ax.grid(axis="x", alpha=0.4)
    plt.tight_layout()
    fig.savefig(OUT / "IMG-05_feature_importance_catboost.png")
    plt.close(fig)
    print("  → IMG-05_feature_importance_catboost.png 저장 완료")
except Exception as e:
    print(f"  → IMG-05 실패: {e}")

# ────────────────────────────────────────────────────────────────────────────
# IMG-06 : ROC 커브 (OvR)
# ────────────────────────────────────────────────────────────────────────────
print("[IMG-06] ROC 커브 생성 중 (시간 걸릴 수 있음)...")
try:
    y_prob_cv = cross_val_predict(
        best_model, X, y, cv=cv, method="predict_proba", n_jobs=-1
    )
    cls_sorted = list(le.classes_)
    y_bin = label_binarize(y, classes=list(range(len(cls_sorted))))

    fig, ax = plt.subplots(figsize=(8, 6))
    aucs = []
    for i, cls in enumerate(cls_sorted):
        fpr, tpr, _ = roc_curve(y_bin[:, i], y_prob_cv[:, i])
        roc_auc = auc(fpr, tpr)
        aucs.append(roc_auc)
        ax.plot(fpr, tpr, color=DC[cls], lw=2,
                label=f"{DKR[cls]}({cls})  AUC={roc_auc:.4f}")

    ax.plot([0,1],[0,1],"k--", lw=1, label="Random (AUC=0.5000)")
    ax.set_xlabel("False Positive Rate", fontsize=11)
    ax.set_ylabel("True Positive Rate", fontsize=11)
    ax.set_title(f"ROC 커브 (OvR) — CatBoost\nMacro AUC = {np.mean(aucs):.4f}",
                 fontsize=12, fontweight="bold")
    ax.legend(loc="lower right", fontsize=10)
    ax.grid(alpha=0.4)
    plt.tight_layout()
    fig.savefig(OUT / "IMG-06_roc_curves.png")
    plt.close(fig)
    print("  → IMG-06_roc_curves.png 저장 완료")
except Exception as e:
    print(f"  → IMG-06 실패: {e}")

# ────────────────────────────────────────────────────────────────────────────
# IMG-07 : 도메인별 F1 바 차트
# ────────────────────────────────────────────────────────────────────────────
print("[IMG-07] 도메인별 F1 생성 중...")
try:
    cls_list = list(le.classes_)
    colors_f1 = [DC[d] for d in cls_list]

    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.bar([DKR[d] for d in cls_list], per_class_f1,
                  color=colors_f1, edgecolor="white", linewidth=1.5, width=0.5)
    ax.axhline(cv_macro_f1, color="gray", ls="--", lw=1.5,
               label=f"Macro F1 평균 ({cv_macro_f1:.4f})")

    for bar, f1 in zip(bars, per_class_f1):
        ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 0.008,
                f"{f1:.4f}", ha="center", va="bottom",
                fontsize=11, fontweight="bold")

    ax.set_ylim(0.8, 1.05)
    ax.set_ylabel("F1-Score", fontsize=11)
    ax.set_title("도메인별 F1-Score — CatBoost (CV 기반, 1000개)\n"
                 "소방 최저 — 배관과 ARC·CIRCLE 구조 공유로 혼동 잔존",
                 fontsize=11, fontweight="bold")
    ax.legend(fontsize=10)
    ax.grid(axis="y", alpha=0.4)
    plt.tight_layout()
    fig.savefig(OUT / "IMG-07_per_class_f1.png")
    plt.close(fig)
    print("  → IMG-07_per_class_f1.png 저장 완료")
except Exception as e:
    print(f"  → IMG-07 실패: {e}")

# ────────────────────────────────────────────────────────────────────────────
# IMG-08 : 6개 모델 CV 박스플롯
# ────────────────────────────────────────────────────────────────────────────
print("[IMG-08] CV 박스플롯 생성 중...")
try:
    model_names = list(cv_results.keys())
    f1_data  = [cv_results[n]["test_macro_f1"] for n in model_names]
    acc_data = [cv_results[n]["test_accuracy"]  for n in model_names]
    palette  = ["#F44336" if n == "CatBoost" else "#90CAF9" for n in model_names]
    n_folds  = len(f1_data[0]) if hasattr(f1_data[0], "__len__") else N_FOLDS

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5.5))
    fig.suptitle(f"모델별 CV 성능 분포 (Stratified {n_folds}-Fold, 데이터 1000개)",
                 fontsize=13, fontweight="bold")

    for ax, data, title in [(ax1, f1_data, "CV Macro F1"), (ax2, acc_data, "CV Accuracy")]:
        bp = ax.boxplot(data, patch_artist=True,
                        medianprops=dict(color="white", linewidth=2),
                        whiskerprops=dict(lw=1.5),
                        capprops=dict(lw=1.5))
        for patch, color in zip(bp["boxes"], palette):
            patch.set_facecolor(color); patch.set_alpha(0.85)
        ax.set_xticklabels(model_names, rotation=12)
        ax.set_title(title); ax.grid(axis="y", alpha=0.4)

    # CatBoost 주석
    if "CatBoost" in model_names:
        cb_idx = model_names.index("CatBoost") + 1  # boxplot은 1-indexed
        ax1.annotate("최종 선정",
                     xy=(cb_idx, np.median(f1_data[model_names.index("CatBoost")])),
                     xytext=(cb_idx + 0.8, np.median(f1_data[model_names.index("CatBoost")]) + 0.02),
                     arrowprops=dict(arrowstyle="->", color="#C62828"),
                     fontsize=9, color="#C62828", fontweight="bold")

    plt.tight_layout()
    fig.savefig(OUT / "IMG-08_cv_boxplot.png")
    plt.close(fig)
    print("  → IMG-08_cv_boxplot.png 저장 완료")
except Exception as e:
    print(f"  → IMG-08 실패: {e}")

# ────────────────────────────────────────────────────────────────────────────
# IMG-09 : 74차원 vs 85차원 성능 비교
# ────────────────────────────────────────────────────────────────────────────
print("[IMG-09] 버전 비교 생성 중...")
try:
    # 74차원 이전 실험 고정값 / 85차원은 현재 결과
    cls_list  = list(le.classes_)
    f1_74_val = 0.807   # 이전 실험 Test Macro F1
    f1_85_val = cv_results["CatBoost"]["test_macro_f1"].mean()  # 현재 CV F1
    # 도메인별: 이전(74차원) 고정값
    pc_74 = {"arch": 0.7956, "elec": 0.8462, "fire": 0.6982, "pipe": 0.8681}
    pc_85 = dict(zip(cls_list, per_class_f1))

    x = np.arange(len(cls_list))
    w = 0.35
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 5))
    fig.suptitle("피처 버전 비교: 74차원 (435개) vs 85차원 (1000개, 증강)",
                 fontsize=12, fontweight="bold")

    ax1.bar(["74차원\n(435개)", "85차원\n(1000개)"],
            [f1_74_val, f1_85_val],
            color=["#90CAF9", "#F44336"], edgecolor="white", width=0.4)
    for xi, val in enumerate([f1_74_val, f1_85_val]):
        ax1.text(xi, val + 0.005, f"{val:.4f}",
                 ha="center", fontsize=13, fontweight="bold")
    ax1.set_ylim(0.6, 1.05); ax1.set_ylabel("Macro F1"); ax1.set_title("전체 Macro F1")

    ax2.bar(x - w/2, [pc_74[d] for d in cls_list], w,
            label="74차원 (435개)", color="#90CAF9", edgecolor="white")
    ax2.bar(x + w/2, [pc_85[d] for d in cls_list], w,
            label="85차원 (1000개)", color="#F44336", edgecolor="white")
    ax2.axhline(0.95, color="gray", ls="--", lw=1, label="0.95 기준")
    ax2.set_xticks(x); ax2.set_xticklabels([DKR[d] for d in cls_list])
    ax2.set_ylim(0.5, 1.08); ax2.set_ylabel("F1-Score"); ax2.set_title("도메인별 F1 비교")
    ax2.legend(fontsize=9)

    plt.tight_layout()
    fig.savefig(OUT / "IMG-09_version_comparison.png")
    plt.close(fig)
    print("  → IMG-09_version_comparison.png 저장 완료")
except Exception as e:
    print(f"  → IMG-09 실패: {e}")

# ────────────────────────────────────────────────────────────────────────────
# IMG-10 : 모델 파이프라인 구조도
# ────────────────────────────────────────────────────────────────────────────
print("[IMG-10] 파이프라인 구조도 생성 중...")
try:
    n_feat = len(feature_names)

    fig, ax = plt.subplots(figsize=(14, 6.5))
    ax.set_xlim(0, 14); ax.set_ylim(0, 6.5)
    ax.axis("off"); ax.set_facecolor("#F8F9FA")
    fig.patch.set_facecolor("#F8F9FA")

    def _box(x, y, w, h, text, color, fs=10):
        ax.add_patch(mpatches.FancyBboxPatch(
            (x-w/2, y-h/2), w, h,
            boxstyle="round,pad=0.25",
            facecolor=color, edgecolor="white", linewidth=2, zorder=3))
        ax.text(x, y, text, ha="center", va="center",
                fontsize=fs, fontweight="bold", color="white",
                zorder=4, multialignment="center")

    def _arr(x1, x2, y, color="#555"):
        ax.annotate("", xy=(x2, y), xytext=(x1, y),
                    arrowprops=dict(arrowstyle="-|>", color=color, lw=2), zorder=2)

    # 블록
    _box(1.1, 3.2, 1.8, 2.0, "CAD JSON\n\n도면명·단위\nlayers[]\nentities[]", "#546E7A", fs=9)
    _arr(2.0, 2.7, 3.2)
    _box(3.6, 3.2, 1.6, 2.4, f"Feature\nExtraction\n\n{n_feat}차원\nnumpy 벡터", "#1565C0", fs=9)

    # 피처 그룹 목록
    groups = ["[A] 엔티티 비율  11차원",  "[B] 이진 존재    3차원",
              "[D] 수치통계      4차원",  "[E] unit 원핫    4차원",
              "[G] 레이어 스타일 6차원",  "[H] AIA 접두어   6차원",
              "[O] 키워드 점수   8차원",  "[P] TEXT 정규식  8차원",
              "[Q-T] 소방배관    9차원",  "기타 그룹       26차원"]
    for i, g in enumerate(groups):
        col, row = i // 5, i % 5
        ax.text(2.75 + col * 1.75, 4.6 - row * 0.4, f"• {g}",
                fontsize=6.2, color="#0D47A1", zorder=5)

    _arr(4.4, 5.15, 3.2)

    # Pipeline
    _box(5.8, 4.2, 1.25, 0.75, "Simple\nImputer", "#00897B", fs=8.5)
    _box(5.8, 3.25, 1.25, 0.75, "Standard\nScaler", "#00897B", fs=8.5)
    _box(5.8, 2.2, 1.25, 0.85, "CatBoost\nClassifier", "#C62828", fs=8.5)
    ax.annotate("", xy=(5.8, 3.62), xytext=(5.8, 3.87),
                arrowprops=dict(arrowstyle="-|>", color="#00897B", lw=1.5))
    ax.annotate("", xy=(5.8, 2.63), xytext=(5.8, 2.87),
                arrowprops=dict(arrowstyle="-|>", color="#C62828", lw=1.5))
    # Pipeline 브래킷
    for _y in [1.7, 4.65]:
        ax.plot([5.1, 5.1], [1.7, 4.65], color="#888", lw=1.2, zorder=2)
    ax.plot([5.1, 5.2], [1.7, 1.7], color="#888", lw=1.2)
    ax.plot([5.1, 5.2], [4.65, 4.65], color="#888", lw=1.2)
    ax.text(4.85, 3.2, "sklearn\nPipeline", ha="center", va="center",
            fontsize=7.5, color="#555", rotation=90)

    _arr(6.45, 7.2, 3.2)

    # 출력
    _box(8.2, 3.2, 1.65, 2.2,
         "확률 출력\n\n{arch: p₁\nelec: p₂\nfire: p₃\npipe: p₄}", "#5E35B1", fs=9)
    _arr(9.05, 9.8, 3.2)

    # 도메인 결과
    sample_probs = {"arch": 0.04, "elec": 0.87, "fire": 0.06, "pipe": 0.03}
    for i, d in enumerate(["arch", "elec", "fire", "pipe"]):
        p = sample_probs[d]
        yi = 4.4 - i * 0.95
        alpha = 1.0 if p > 0.5 else 0.45
        ax.add_patch(mpatches.FancyBboxPatch(
            (9.8, yi-0.32), 3.6, 0.64,
            boxstyle="round,pad=0.1",
            facecolor=DC[d], alpha=alpha, edgecolor="white", zorder=3))
        prefix = "▶ " if p > 0.5 else "     "
        ax.text(11.6, yi,
                f"{prefix}{DKR[d]}({d})   {p:.0%}",
                ha="center", va="center",
                fontsize=9.5, fontweight="bold" if p > 0.5 else "normal",
                color="white", zorder=4)

    ax.text(7.0, 6.1, "도메인 자동 분류기 — 추론 파이프라인 구조",
            ha="center", va="center", fontsize=13, fontweight="bold", color="#212121")

    plt.tight_layout()
    fig.savefig(OUT / "IMG-10_pipeline_diagram.png", facecolor=fig.get_facecolor())
    plt.close(fig)
    print("  → IMG-10_pipeline_diagram.png 저장 완료")
except Exception as e:
    print(f"  → IMG-10 실패: {e}")

# ── 완료 요약 ────────────────────────────────────────────────────────────────
print()
print("=" * 60)
generated = sorted(OUT.glob("IMG-*.png"))
print(f"  생성 완료: {len(generated)}개")
for p in generated:
    print(f"    {p.name}")
print(f"  저장 경로: {OUT}")
print("=" * 60)
