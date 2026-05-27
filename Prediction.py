import numpy as np
from sklearn.model_selection import train_test_split, KFold, cross_val_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler
from sklearn.linear_model import RidgeCV
import random

from models import CreateModel, Ticket, Iteration, Event


class Prediction:
    def __init__(self):
        pass

    def get_Section(self, section,event_title):
        SessionLocal = CreateModel().getSession()
        x = []
        y = []
        with SessionLocal() as s:
            tickets = (
                s.query(Ticket)
                .join(Ticket.iteration)
                .join(Iteration.event)
                .filter(
                    Ticket.section == section,
                    Event.title == event_title).all())

            for t in tickets:
                # 1) Iteration for this ticket
                it = t.iteration
                when_captured = it.captured_at

                # 2) Event for this ticket (via iteration)
                ev = t.iteration.event.event_date
                x.append([round((ev - when_captured).total_seconds() / 3600,1),t.ticketsPerSection])
                y.append(t.price)

            return x, y

    def degree_finder(self,xData,yData):
        lowestRMSE = 100
        lowest_degree = 0
        for i in range(4):
            RMSE, degree = self.not_manual(xData,yData, i)
            if RMSE < lowestRMSE:
                lowestRMSE = RMSE
                lowest_degree = degree
        return lowest_degree


    def not_manual(self,xData,yData,degree):
        # Step 1: Full data
        X_full = np.array(xData, dtype=float)
        y_full = np.array(yData, dtype=float)

        # Step 2: Hold out a final test set
        X_train, X_test, y_train, y_test = train_test_split(
            X_full, y_full, test_size=0.25, random_state=0
        )

        # Step 3: Run K-Fold CV on the training set
        pipeline = Pipeline([
            ("poly", PolynomialFeatures(degree=degree, include_bias=False)),
            ("scaler", StandardScaler()),
            ("ridge", RidgeCV(alphas=np.logspace(-3, 3, 25), cv=2))
        ])

        cv = KFold(n_splits=5, shuffle=True, random_state=42)
        scores = cross_val_score(pipeline, X_train, y_train,
                                 cv=cv, scoring="neg_root_mean_squared_error")

        rmse_per_fold = -scores
        rmse_mean = rmse_per_fold.mean()
        print("Per-fold RMSE:", rmse_per_fold)
        print("Average RMSE:", rmse_mean)

        return rmse_mean,degree

    def train_model(self, xData, yData, best_degree):
        X = np.array(xData, dtype=float)
        y = np.array(yData, dtype=float)

        # Optionally split the data to hold out a test set
        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=0)

        pipeline = Pipeline([
            ("poly", PolynomialFeatures(degree=best_degree, include_bias=False)),
            ("scaler", StandardScaler()),
            ("ridge", RidgeCV(alphas=np.logspace(-3, 3, 25), cv=2))
        ])

        pipeline.fit(X_train, y_train)
        return pipeline


    # def prediction(self,xData, yData,degree,num_validations):
    #     X = np.array(xData, dtype=float)
    #     y = np.array(yData, dtype=float)
    #
    #     X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=0)
    #
    #     self.validation_training_split(X_train,y_train,num_validations,degree)
    #
    # #Manually finds the error for this given model (how far each prediction is on average from the true Y)
    # def RMSE(self, y_test,y_predicted):
    #     totalError = 0
    #     for i in range(len(y_test)):
    #         totalError += (y_test[i] - y_predicted[i]) ** 2
    #     totalError /= len(y_test)
    #     totalError = totalError ** 0.5
    #     print(totalError)
    #     return totalError
    #
    #
    #
    # def validation_training_split(self, x_train_data: list, y_train_data: list, num_validations: int,degree):
    #     # 1. Shuffle X and Y in the same order
    #     combined = list(zip(x_train_data, y_train_data))
    #     random.shuffle(combined)
    #     x_shuffled, y_shuffled = zip(*combined)
    #     x_shuffled, y_shuffled = list(x_shuffled), list(y_shuffled)
    #
    #     # 2. Size of each fold
    #     num_per_iteration = int(len(x_shuffled) / num_validations)
    #     rmse_scores = []
    #
    #     # 3. Loop through each fold
    #     for fold in range(num_validations):
    #         start_idx = fold * num_per_iteration
    #         end_idx = start_idx + num_per_iteration
    #
    #         # Validation split
    #         X_val = x_shuffled[start_idx:end_idx]
    #         y_val = y_shuffled[start_idx:end_idx]
    #
    #         # Training split (everything else)
    #         X_train = x_shuffled[:start_idx] + x_shuffled[end_idx:]
    #         y_train = y_shuffled[:start_idx] + y_shuffled[end_idx:]
    #
    #         # Fit polynomial on training set only
    #         poly = PolynomialFeatures(degree=degree, include_bias=False)
    #         X_train_poly = poly.fit_transform(X_train)
    #         X_val_poly = poly.transform(X_val)
    #
    #         # Fit scaler on training set only
    #         scaler = StandardScaler()
    #         X_train_poly_scaled = scaler.fit_transform(X_train_poly)
    #         X_val_poly_scaled = scaler.transform(X_val_poly)
    #
    #         # 4. Train the model
    #         ridge = RidgeCV(alphas=np.logspace(-3, 3, 25), cv=2)
    #         ridge.fit(X_train_poly_scaled, y_train)
    #
    #         # 5. Predict & RMSE
    #         y_pred = ridge.predict(X_val_poly_scaled).tolist()
    #         rmse = self.RMSE(y_val, y_pred)
    #         rmse_scores.append(rmse)
    #
    #     # 6. Average RMSE across folds
    #     avg_rmse = sum(rmse_scores) / len(rmse_scores)
    #     print(f"Average RMSE over {num_validations} folds: {avg_rmse}")
    #     return avg_rmse

