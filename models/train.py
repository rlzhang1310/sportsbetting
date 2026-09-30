import pandas as pd
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import log_loss

# 1. Load the games.
# A = home team, B = away team.
data = pd.read_csv("./data/sample.csv")

# 2. Create six features: home rating minus away rating.
ratings = [
    "Off_pass",
    "Off_run",
    "Off_passblock",
    "Def_pass",
    "Def_run",
    "Def_passrush",
]

X = pd.DataFrame()

for rating in ratings:
    X[rating] = data["A_" + rating] - data["B_" + rating]

# What we want to predict: 1 = home win, 0 = away win.
y = data["A_win"]

# 3. Train on 2023. Test on 2024.
X_train = X[data["season"] == 2023]
y_train = y[data["season"] == 2023]

X_test = X[data["season"] == 2024]
y_test = y[data["season"] == 2024]

# 4. Put features on comparable scales.
# Learn the scaling from training data only.
scaler = StandardScaler()
X_train_scaled = scaler.fit_transform(X_train)
X_test_scaled = scaler.transform(X_test)

# 5. Train logistic regression.
# Fix C for now so we can focus on understanding the model.
model = LogisticRegression(C=1.0, max_iter=2000)
model.fit(X_train_scaled, y_train)

# 6. Predict home-win probabilities for the 2024 games.
# predict_proba returns [away-win probability, home-win probability].
probabilities = model.predict_proba(X_test_scaled)[:, 1]

# 7. Compare against always predicting the 2023 home-win rate.
baseline_probability = y_train.mean()
baseline_predictions = [baseline_probability] * len(y_test)

print("Lower log loss is better.")
print("Baseline:", round(log_loss(y_test, baseline_predictions), 4))
print("Model:   ", round(log_loss(y_test, probabilities), 4))

# 8. Show a few predictions alongside actual outcomes.
results = data.loc[
    data["season"] == 2024,
    ["game_date", "A_team", "B_team", "A_win"],
].copy()

results["home_win_probability"] = probabilities

print(results.head(10).to_string(index=False))