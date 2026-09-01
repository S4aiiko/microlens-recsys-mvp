"""Stable, training-free ModelBundle serving boundary."""

from .runtime import LoadedRecommendationModel, load_recommendation_model

__all__ = ["LoadedRecommendationModel", "load_recommendation_model"]
