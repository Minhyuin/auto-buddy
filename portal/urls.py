"""URLs for the administrator portal."""

from django.urls import path

from . import views

app_name = "portal"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("accounts/new/", views.account_create, name="account_create"),
    path("accounts/<uuid:pk>/", views.account_detail, name="account_detail"),
    path("accounts/<uuid:pk>/edit/", views.account_edit, name="account_edit"),
    path("accounts/<uuid:pk>/schedule/", views.account_schedule, name="account_schedule"),
    path("accounts/<uuid:pk>/run/", views.account_run, name="account_run"),
    path("accounts/<uuid:pk>/check/", views.account_check, name="account_check"),
    path("accounts/<uuid:pk>/delete/", views.account_delete, name="account_delete"),
]
