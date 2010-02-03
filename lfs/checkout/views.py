# django imports
from django.conf import settings
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.core.urlresolvers import reverse
from django.http import HttpResponse
from django.http import HttpResponseRedirect
from django.shortcuts import render_to_response
from django.template.loader import render_to_string
from django.template import RequestContext
from django.utils import simplejson
from django.utils.translation import ugettext_lazy as _

# lfs imports
import lfs.core.utils
import lfs.order.utils
import lfs.payment.utils
import lfs.shipping.utils
import lfs.voucher.utils
from lfs.cart import utils as cart_utils
from lfs.checkout.forms import OnePageCheckoutForm
from lfs.checkout.settings import CHECKOUT_TYPE_ANON
from lfs.checkout.settings import CHECKOUT_TYPE_AUTH
from lfs.checkout.settings import INVOICE_PREFIX, SHIPPING_PREFIX
from lfs.core.settings import LFS_ADDRESS_L10N
from lfs.customer import utils as customer_utils
from lfs.customer.models import BankAccount
from lfs.customer.forms import RegisterForm
from lfs.customer.views import address_inline, save_address
from lfs.payment.models import PaymentMethod
from lfs.payment.settings import PAYPAL
from lfs.payment.settings import DIRECT_DEBIT
from lfs.payment.settings import CREDIT_CARD
from lfs.voucher.models import Voucher
from lfs.voucher.settings import MESSAGES

# other imports
from postal.models import PostalAddress
from postal.library import get_postal_form_class
from postal.forms import PostalAddressForm
from countries.models import Country

def login(request, template_name="lfs/checkout/login.html"):
    """Displays a form to login or register/login the user within the check out
    process.

    The form's post request goes to lfs.customer.views.login where all the logic
    happens - see there for more.
    """
    # If the user is already authenticate we don't want to show this view at all
    if request.user.is_authenticated():
        return HttpResponseRedirect(reverse("lfs_checkout"))

    shop = lfs.core.utils.get_default_shop()

    # If only anonymous checkout allowed we don't want to show this view at all.
    if shop.checkout_type == CHECKOUT_TYPE_ANON:
        return HttpResponseRedirect(reverse("lfs_checkout"))

    # Using Djangos default AuthenticationForm
    login_form = AuthenticationForm()
    login_form.fields["username"].label = _(u"E-Mail")
    register_form = RegisterForm()

    if request.POST.get("action") == "login":
        login_form = AuthenticationForm(data=request.POST)
        if login_form.is_valid():
            from django.contrib.auth import login
            login(request, login_form.get_user())

            return lfs.core.utils.set_message_cookie(reverse("lfs_checkout"),
                msg = _(u"You have been logged in."))

    elif request.POST.get("action") == "register":
        register_form = RegisterForm(data=request.POST)
        if register_form.is_valid():
            email = register_form.data.get("email")
            password = register_form.data.get("password_1")

            # Create user
            user = User.objects.create_user(
                username=email, email=email, password=password)

            # Notify
            lfs.core.signals.customer_added.send(user)

            # Log in user
            from django.contrib.auth import authenticate
            user = authenticate(username=email, password=password)

            from django.contrib.auth import login
            login(request, user)

            return lfs.core.utils.set_message_cookie(reverse("lfs_checkout"),
                msg = _(u"You have been registered and logged in."))

    return render_to_response(template_name, RequestContext(request, {
        "login_form" : login_form,
        "register_form" : register_form,
        "anonymous_checkout" : shop.checkout_type != CHECKOUT_TYPE_AUTH,
    }))

def checkout_dispatcher(request):
    """Dispatcher to display the correct checkout form
    """
    shop = lfs.core.utils.get_default_shop()
    cart = cart_utils.get_cart(request)

    if cart is None or not cart.items():
        return empty_page_checkout(request)

    if request.user.is_authenticated() or \
       shop.checkout_type == CHECKOUT_TYPE_ANON:
        return HttpResponseRedirect(reverse("lfs_checkout"))
    else:
        return HttpResponseRedirect(reverse("lfs_checkout_login"))

def cart_inline(request, template_name="lfs/checkout/checkout_cart_inline.html"):
    """Displays the cart items of the checkout page.

    Factored out to be reusable for the starting request (which renders the
    whole checkout page and subsequent ajax requests which refresh the
    cart items.
    """
    cart = cart_utils.get_cart(request)

    # Shipping
    selected_shipping_method = lfs.shipping.utils.get_selected_shipping_method(request)
    shipping_costs = lfs.shipping.utils.get_shipping_costs(request, selected_shipping_method)

    # Payment
    selected_payment_method = lfs.payment.utils.get_selected_payment_method(request)
    payment_costs = lfs.payment.utils.get_payment_costs(request, selected_payment_method)

    # Cart costs
    cart_costs = cart_utils.get_cart_costs(request, cart)
    cart_price = cart_costs["price"] + shipping_costs["price"] + payment_costs["price"]
    cart_tax = cart_costs["tax"] + shipping_costs["tax"] + payment_costs["tax"]

    # Voucher
    try:
        voucher_number = lfs.voucher.utils.get_current_voucher_number(request)
        voucher = Voucher.objects.get(number=voucher_number)
    except Voucher.DoesNotExist:
        display_voucher = False
        voucher_value = 0
        voucher_tax = 0
        voucher_message = MESSAGES[6]
    else:
        lfs.voucher.utils.set_current_voucher_number(request, voucher_number)
        is_voucher_effective, voucher_message = voucher.is_effective(cart)
        if is_voucher_effective:
            display_voucher = True
            voucher_value = voucher.get_price_gross(cart)
            cart_price = cart_price - voucher_value
            voucher_tax = voucher.get_tax(cart)
        else:
            display_voucher = False
            voucher_value = 0
            voucher_tax = 0

    return render_to_string(template_name, RequestContext(request, {
        "cart" : cart,
        "cart_price" : cart_price,
        "cart_tax" : cart_tax,
        "display_voucher" : display_voucher,
        "voucher_value" : voucher_value,
        "voucher_tax" : voucher_tax,
        "shipping_price" : shipping_costs["price"],
        "payment_price" : payment_costs["price"],
        "selected_shipping_method" : selected_shipping_method,
        "selected_payment_method" : selected_payment_method,
        "voucher_number" : voucher_number,
        "voucher_message" : voucher_message,
    }))

def one_page_checkout(request, checkout_form = OnePageCheckoutForm,
    template_name="lfs/checkout/one_page_checkout.html"):
    """One page checkout form.
    """
    # If the user is not authenticated and the if only authenticate checkout
    # allowed we rediret to authentication page.
    shop = lfs.core.utils.get_default_shop()
    if request.user.is_anonymous() and \
       shop.checkout_type == CHECKOUT_TYPE_AUTH:
        return HttpResponseRedirect(reverse("lfs_checkout_login"))

    customer = customer_utils.get_or_create_customer(request)
    if request.method == "POST":
        # map our ajax fields to our OnePageCheckoutForm fields
        extra_data = {}
        prefixes = [INVOICE_PREFIX, SHIPPING_PREFIX]
        for prefix in prefixes:
            for field_name in PostalAddressForm.base_fields.keys():
                field_value = request.POST.get(prefix + '-' + field_name, None)
                if field_value is not None:
                    extra_data.update({prefix + '_' + field_name:field_value})

        mutable_rp = request.POST.copy()
        mutable_rp.update(extra_data)
        form = checkout_form(mutable_rp)

        if form.is_valid():
            # Create or update invoice address
            save_address(request, customer, INVOICE_PREFIX)

            # save invoice phone and email
            customer.selected_invoice_phone = request.POST.get("invoice_phone")
            customer.selected_invoice_email = request.POST.get("invoice_email")

            # If the shipping address differs from invoice firstname we create
            # or update the shipping address.
            if not form.cleaned_data.get("no_shipping"):
                save_address(request, customer, SHIPPING_PREFIX)

                # save shipping phone and email
                customer.selected_shipping_phone = request.POST.get("shipping_phone")
                customer.selected_shipping_email = request.POST.get("shipping_email")

            # Payment method
            customer.selected_payment_method_id = request.POST.get("payment_method")

            # 1 = Direct Debit
            if customer.selected_payment_method_id is not None:
                if int(customer.selected_payment_method_id) == DIRECT_DEBIT:
                    bank_account = BankAccount.objects.create(
                        account_number = form.cleaned_data.get("account_number"),
                        bank_identification_code = form.cleaned_data.get("bank_identification_code"),
                        bank_name = form.cleaned_data.get("bank_name"),
                        depositor = form.cleaned_data.get("depositor"),
                    )

                    customer.selected_bank_account = bank_account

            # Save the selected information to the customer
            customer.save()

            # process the payment method ...
            result = lfs.payment.utils.process_payment(request)

            payment_method = lfs.payment.utils.get_selected_payment_method(request)

            # Only if the payment is succesful we create the order out of the
            # cart.
            if result.get("success") == True:
                order = lfs.order.utils.add_order(request)

                # TODO: Get rid of these payment specific payment stuff. This
                # should be within payment utils.
                if payment_method.id == PAYPAL and settings.LFS_PAYPAL_REDIRECT:
                    return HttpResponseRedirect(order.get_pay_link())
                else:
                    return HttpResponseRedirect(result.get("next-url"))
            else:
                if result.has_key("message"):
                    form._errors[result.get("message-key")] = result.get("message")

        else: # form is not valid
            # Create or update invoice address
            save_address(request, customer, INVOICE_PREFIX)

            # save invoice phone and email
            customer.selected_invoice_phone = request.POST.get("invoice_phone")
            customer.selected_invoice_email = request.POST.get("invoice_email")

            # If the shipping address differs from invoice firstname we create
            # or update the shipping address.
            if not form.data.get("no_shipping"):
                save_address(request, customer, SHIPPING_PREFIX)

                # save shipping phone and email
                customer.selected_shipping_phone = request.POST.get("shipping_phone")
                customer.selected_shipping_email = request.POST.get("shipping_email")

            # Payment method
            customer.selected_payment_method_id = request.POST.get("payment_method")

            # 1 = Direct Debit
            if customer.selected_payment_method_id:
                if int(customer.selected_payment_method_id) == DIRECT_DEBIT:
                    bank_account = BankAccount.objects.create(
                        account_number = form.data.get("account_number"),
                        bank_identification_code = form.data.get("bank_identification_code"),
                        bank_name = form.data.get("bank_name"),
                        depositor = form.data.get("depositor"),
                    )

                    customer.selected_bank_account = bank_account

            # Save the selected information to the customer
            customer.save()

    else:
        # If there are addresses intialize the form.
        initial = {"no_shipping" : False,}
        form = checkout_form(initial=initial)

    cart = cart_utils.get_cart(request)
    if cart is None:
        return HttpResponseRedirect(reverse('lfs_cart'))

    # Payment
    try:
        selected_payment_method_id = request.POST.get("payment_method")
        selected_payment_method = PaymentMethod.objects.get(pk=selected_payment_method_id)
    except PaymentMethod.DoesNotExist:
        selected_payment_method = lfs.payment.utils.get_selected_payment_method(request)

    valid_payment_methods = lfs.payment.utils.get_valid_payment_methods(request)
    valid_payment_method_ids = [m.id for m in valid_payment_methods]

    display_bank_account = DIRECT_DEBIT in valid_payment_method_ids
    display_credit_card = CREDIT_CARD in valid_payment_method_ids

    response = render_to_response(template_name, RequestContext(request, {
        "form" : form,
        "cart_inline" : cart_inline(request),
        "shipping_inline" : shipping_inline(request),
        "invoice_address_inline" : address_inline(request, INVOICE_PREFIX, form),
        "shipping_address_inline" : address_inline(request, SHIPPING_PREFIX, form),
        "payment_inline" : payment_inline(request, form),
        "selected_payment_method" : selected_payment_method,
        "display_bank_account" : display_bank_account,
        "display_credit_card" : display_credit_card,
        "voucher_number" : lfs.voucher.utils.get_current_voucher_number(request),
    }))

    if form._errors:
        return lfs.core.utils.set_message_to(response, _(u"An error has been occured."))
    else:
        return response

def empty_page_checkout(request, template_name="lfs/checkout/empty_page_checkout.html"):
    """
    """
    return render_to_response(template_name, RequestContext(request, {
        "shopping_url" : reverse("lfs.core.views.shop_view"),
    }))

def thank_you(request, template_name="lfs/checkout/thank_you_page.html"):
    """Displays a thank you page ot the customer
    """
    order = request.session.get("order")
    return render_to_response(template_name, RequestContext(request, {
        "order" : order,
    }))

def payment_inline(request, form, template_name="lfs/checkout/payment_inline.html"):
    """Displays the selectable payment methods of the checkout page.

    Factored out to be reusable for the starting request (which renders the
    whole checkout page and subsequent ajax requests which refresh the
    selectable payment methods.

    Passing the form to be able to display payment forms within the several
    payment methods, e.g. credit card form.
    """
    # Payment
    try:
        selected_payment_method_id = request.POST.get("payment_method")
        selected_payment_method = PaymentMethod.objects.get(pk=selected_payment_method_id)
    except PaymentMethod.DoesNotExist:
        selected_payment_method = lfs.payment.utils.get_selected_payment_method(request)

    valid_payment_methods = lfs.payment.utils.get_valid_payment_methods(request)
    display_bank_account = DIRECT_DEBIT in [m.id for m in valid_payment_methods]

    return render_to_string(template_name, RequestContext(request, {
        "payment_methods" : valid_payment_methods,
        "selected_payment_method" : selected_payment_method,
        "form" : form,
    }))

def shipping_inline(request, template_name="lfs/checkout/shipping_inline.html"):
    """Displays the selectable shipping methods of the checkout page.

    Factored out to be reusable for the starting request (which renders the
    whole checkout page and subsequent ajax requests which refresh the
    selectable shipping methods.
    """
    selected_shipping_method = lfs.shipping.utils.get_selected_shipping_method(request)
    shipping_methods = lfs.shipping.utils.get_valid_shipping_methods(request)

    return render_to_string(template_name, RequestContext(request, {
        "shipping_methods" : shipping_methods,
        "selected_shipping_method" : selected_shipping_method,
    }))


def check_voucher(request):
    """
    """
    voucher_number = lfs.voucher.utils.get_current_voucher_number(request)
    lfs.voucher.utils.set_current_voucher_number(request, voucher_number)

    result = simplejson.dumps({
        "html" : (("#cart-inline", cart_inline(request)),)
    })

    return HttpResponse(result)

def changed_checkout(request):
    """
    """
    form = OnePageCheckoutForm()
    customer = customer_utils.get_or_create_customer(request)
    _save_customer(request, customer)
    _save_country(request, customer)

    result = simplejson.dumps({
        SHIPPING_PREFIX : shipping_inline(request),
        "payment" : payment_inline(request, form),
        "cart" : cart_inline(request),
    })

    return HttpResponse(result)

def changed_invoice_country(request):
    """
    """
    form = OnePageCheckoutForm(request.POST)
    result = simplejson.dumps({
        "invoice_address" : address_inline(request, INVOICE_PREFIX, form),
    })
    return HttpResponse(result)

def changed_shipping_country(request):
    """
    """
    form = OnePageCheckoutForm(request.POST)
    result = simplejson.dumps({
        "shipping_address" : address_inline(request, SHIPPING_PREFIX, form),
    })

    return HttpResponse(result)

def _save_country(request, customer):
    """
    """
    # Update shipping country
    country = request.POST.get("shipping-country")
    if request.POST.get("no_shipping") == "on":
        country = request.POST.get("invoice-country")

    if customer.selected_shipping_address:
        customer.selected_shipping_address.country_id = country
        customer.selected_shipping_address.save()
    customer.selected_country_id = country
    customer.save()

    lfs.shipping.utils.update_to_valid_shipping_method(request, customer)
    lfs.payment.utils.update_to_valid_payment_method(request, customer)
    customer.save()

def _save_customer(request, customer):
    """
    """
    shipping_method = request.POST.get("shipping-method")
    customer.selected_shipping_method_id = shipping_method

    payment_method = request.POST.get("payment_method")
    customer.selected_payment_method_id = payment_method

    customer.save()

    lfs.shipping.utils.update_to_valid_shipping_method(request, customer)
    lfs.payment.utils.update_to_valid_payment_method(request, customer)
    customer.save()