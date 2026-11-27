import joblib
import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, f1_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset

from src.config import BEST_NN_PATH, BEST_NN_SCALER_PATH, PROCESSED_DATA_PATH
from src.neural.neural_network import CreditDefaultMLP

# Фиксируем seed для воспроизводимости
torch.manual_seed(42)
np.random.seed(42)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# Загрузка данных (укажите путь к вашему CSV-файлу)
df = pd.read_csv(PROCESSED_DATA_PATH)
df = df.drop(columns=["id"])

target_col = "default"
X = df.drop(columns=[target_col]).values.astype(np.float32)
y = df[target_col].values.astype(np.float32)

# Стратифицированное разбиение (учитывает дисбаланс классов ~22% vs 78%)
X_train, X_test, y_train, y_test = train_test_split(
    X, y, test_size=0.2, random_state=42, stratify=y
)

# Масштабирование признаков только по обучающей выборке
scaler = StandardScaler()
X_train = scaler.fit_transform(X_train)
X_test = scaler.transform(X_test)

train_ds = TensorDataset(torch.tensor(X_train), torch.tensor(y_train).unsqueeze(1))
test_ds = TensorDataset(torch.tensor(X_test), torch.tensor(y_test).unsqueeze(1))

train_loader = DataLoader(train_ds, batch_size=256, shuffle=True)
test_loader = DataLoader(test_ds, batch_size=512, shuffle=False)

model = CreditDefaultMLP(in_features=X_train.shape[1]).to(device)

# Вес положительного класса: (число отрицательных / число положительных) на train
num_pos = y_train.sum()
num_neg = len(y_train) - num_pos
pos_weight = torch.tensor([num_neg / num_pos], dtype=torch.float32, device=device)

criterion = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3, weight_decay=1e-4)


epochs = 25

for epoch in range(1, epochs + 1):
    model.train()
    train_loss = 0.0

    for xb, yb in train_loader:
        xb, yb = xb.to(device), yb.to(device)
        optimizer.zero_grad()
        logits = model(xb)
        loss = criterion(logits, yb)
        loss.backward()
        optimizer.step()
        train_loss += loss.item() * len(xb)

    train_loss /= len(train_ds)

    model.eval()
    probs_list, targets_list = [], []
    with torch.no_grad():
        for xb, yb in test_loader:
            xb = xb.to(device)
            probs = torch.sigmoid(model(xb)).cpu().numpy().ravel()
            probs_list.append(probs)
            targets_list.append(yb.numpy().ravel())

    y_probs = np.concatenate(probs_list)
    y_true = np.concatenate(targets_list)
    y_pred = (y_probs >= 0.5).astype(int)

    roc_auc = roc_auc_score(y_true, y_probs)
    f1 = f1_score(y_true, y_pred)
    acc = accuracy_score(y_true, y_pred)

    if epoch % 5 == 0 or epoch == 1:
        print(
            f"Epoch {epoch:02d}/{epochs} | Loss: {train_loss:.4f} | "
            f"ROC-AUC: {roc_auc:.4f} | F1: {f1:.4f} | Acc: {acc:.4f}"
        )
# Сохраняем веса модели и скейлер
print("Cохраняем модель и скейлер")
torch.save(model.state_dict(), BEST_NN_PATH)
joblib.dump(scaler, BEST_NN_SCALER_PATH)
