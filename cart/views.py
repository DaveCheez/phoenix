from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from rest_framework import status
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from .models import Cart, CartItem
from store.models import Product
from .serializers import CartSerializer
import traceback

@api_view(['POST'])
def create_cart(request):
    # Ensure session exists
    if not request.session.session_key:
        request.session.create()
    
    session_key = request.session.session_key
    
    # Check if a cart already exists for this session
    cart, created = Cart.objects.get_or_create(session_key=session_key)
    
    response = Response({'cart_id': str(cart.id)})
    # Set the cookie on the response. max_age is in seconds (e.g., 30 days)
    response.set_cookie('cart_id', str(cart.id), max_age=30*24*60*60, samesite='Lax')
    return response

@api_view(['GET'])
def get_cart(request):
    cart_id = request.query_params.get('cart_id')

    if not cart_id:
        return Response({'error': 'Missing cart_id'}, status=400)

    try:
        cart = Cart.objects.get(id=cart_id)
    except Cart.DoesNotExist:
        return Response({'error': 'Cart not found'}, status=404)

    items = CartItem.objects.filter(cart=cart)

    cart_items = []
    cart_total = 0

    for item in items:
        unit_price = item.product.price or 0
        line_total = unit_price * item.quantity
        cart_total += line_total

        cart_items.append({
            'item_id': item.id,
            'product_id': item.product.id,
            'name': item.product.name,
            'product_slug': item.product.slug,
            'quantity': item.quantity,
            'price': str(unit_price),
            'sku': getattr(item.product, 'sku', None),
            'image': item.product.get_featured_image_url() or '',
        })

    return Response({
        'success': True,
        'cart': {
            'id': str(cart.id),
            'items': cart_items,
            'total': str(cart_total),  # 👈 Grand total
        }
    })


@api_view(['POST'])
def add_to_cart(request):
    try:
        cart_id = request.data.get('cart_id')
        product_id = request.data.get('product_id')
        quantity = int(request.data.get('quantity', 1))

        if not cart_id:
            return Response({'error': 'Missing cart_id'}, status=400)

        # Try to get cart by ID, or by session if ID is not found (for robustness)
        try:
            cart = Cart.objects.get(id=cart_id)
        except Cart.DoesNotExist:
            session_key = request.session.session_key
            cart, _ = Cart.objects.get_or_create(session_key=session_key)

        product = Product.objects.get(id=product_id)

        item, created = CartItem.objects.get_or_create(cart=cart, product=product)
        if not created:
            item.quantity += quantity
        else:
            item.quantity = quantity
        item.save()

        return Response({'success': True})
    
    except Exception as e:
        traceback.print_exc()  # Log full stack trace to terminal
        return Response({'error': str(e)}, status=500)

@api_view(['POST'])
def update_cart_item(request):
    item_id = request.data.get('item_id')
    cart_id = request.data.get('cart_id')

    try:
        quantity = int(request.data.get('quantity', 1))
    except (TypeError, ValueError):
        return Response({'error': 'Quantity must be a whole number'}, status=400)

    if not item_id or not cart_id:
        return Response({'error': 'Missing item_id or cart_id'}, status=400)

    if quantity < 1:
        return Response({'error': 'Quantity must be at least 1'}, status=400)

    try:
        item = CartItem.objects.get(id=item_id, cart_id=cart_id)
        item.quantity = quantity
        item.save(update_fields=['quantity'])
        return Response({'success': True})
    except CartItem.DoesNotExist:
        return Response({'error': 'Item not found'}, status=404)

@api_view(['POST'])
def remove_from_cart(request):
    item_id = request.data.get('item_id')
    cart_id = request.data.get('cart_id')

    if not item_id or not cart_id:
        return Response({'error': 'Missing item_id or cart_id'}, status=400)

    try:
        item = CartItem.objects.get(id=item_id, cart_id=cart_id)
        item.delete()
        return Response({'success': True})
    except CartItem.DoesNotExist:
        return Response({'error': 'Item not found'}, status=404)
