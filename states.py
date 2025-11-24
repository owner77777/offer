from aiogram.fsm.state import State, StatesGroup

class AdSubmission(StatesGroup):
    waiting_for_start_button = State()
    waiting_for_item_desc = State()
    waiting_for_price = State()
    waiting_for_contact = State()
    waiting_for_confirmation = State()
    waiting_for_edit_desc = State()
    waiting_for_edit_price = State()
    waiting_for_edit_contact = State()

class Broadcast(StatesGroup):
    waiting_for_message = State()
    waiting_for_confirmation = State()

class Stats(StatesGroup):
    initial = State()
