from django.urls import path
from . import views

urlpatterns = [
    path('', views.ai_generate, name='ai_generate'),
    path('download-pdf/', views.download_pdf, name='download_pdf'),
    path('signup/', views.signup, name='signup'),
   
   
]