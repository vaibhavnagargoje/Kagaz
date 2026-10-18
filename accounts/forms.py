from django import forms
from django.core.validators import RegexValidator

pin_format = RegexValidator(r"^\d{4,6}$", "Use 4 to 6 digits.")

WEAK_PINS = {"0000", "1111", "1234", "4321", "000000", "111111", "123456", "654321", "123123"}


def pin_field(label: str, autofocus: bool = False) -> forms.CharField:
    attrs = {"inputmode": "numeric", "autocomplete": "off", "maxlength": 6, "pattern": "[0-9]*", "class": "input pin-input"}
    if autofocus:
        attrs["autofocus"] = True
    return forms.CharField(label=label, validators=[pin_format], widget=forms.PasswordInput(attrs=attrs))


class EnterPinForm(forms.Form):
    pin = pin_field("Your PIN", autofocus=True)


class SetPinForm(forms.Form):
    pin = pin_field("New PIN (4–6 digits)", autofocus=True)
    pin_confirm = pin_field("Type it again")

    def clean(self):
        cleaned = super().clean()
        pin = cleaned.get("pin")
        if pin and (pin in WEAK_PINS or len(set(pin)) == 1):
            self.add_error("pin", "That PIN is too easy to guess. Pick another.")
        elif pin and pin != cleaned.get("pin_confirm"):
            self.add_error("pin_confirm", "The two PINs are different.")
        return cleaned


class ChangePinForm(SetPinForm):
    current_pin = pin_field("Current PIN", autofocus=True)

    field_order = ["current_pin", "pin", "pin_confirm"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["pin"].widget.attrs.pop("autofocus", None)


# The forms behind WhatsApp links also sign the browser in, so they ask
# whether this is someone else's computer.

shared_device_field = forms.BooleanField(
    required=False, label="This is a shared or cyber café computer (sign out automatically)"
)


class LinkEnterPinForm(EnterPinForm):
    shared_device = shared_device_field


class LinkSetPinForm(SetPinForm):
    shared_device = shared_device_field


class LinkChangePinForm(ChangePinForm):
    shared_device = shared_device_field

    field_order = ["current_pin", "pin", "pin_confirm", "shared_device"]
