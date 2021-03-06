import asyncio
import io
import logging
from datetime import datetime
from os import path

import pytz
from aiogram import Bot, types
from aiogram.contrib.fsm_storage.redis import RedisStorage2
from aiogram.dispatcher import Dispatcher, FSMContext
from aiogram.utils import executor
from aiogram.utils.exceptions import InvalidQueryID
from disposable_email_domains import blocklist

import config
from locator import Locator
from mail_verifier import MailVerifier
from mailer import Mailer
from photoitem import PhotoItem
from states import Form
from uploader import Uploader
from locales import Locales

mailer = Mailer(config.SIB_ACCESS_KEY)
locator = Locator()
mail_verifier = MailVerifier()
uploader = Uploader()
semaphore = asyncio.Semaphore()
locales = Locales()


def setup_logging():
    # create logger
    my_logger = logging.getLogger('parkun_log')
    my_logger.setLevel(logging.DEBUG)

    # create file handler which logs even debug messages
    # fh = logging.FileHandler(config.LOG_PATH)
    # fh.setLevel(logging.DEBUG)

    # create console handler with a higher log level
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)

    # create formatter and add it to the handlers
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    # fh.setFormatter(formatter)
    ch.setFormatter(formatter)

    # add the handlers to the logger
    # logger.addHandler(fh)
    my_logger.addHandler(ch)

    return my_logger


loop = asyncio.get_event_loop()
bot = Bot(token=config.API_TOKEN, loop=loop)

storage = RedisStorage2(host=config.REDIS_HOST,
                        port=config.REDIS_PORT)

dp = Dispatcher(bot, storage=storage)

logger = setup_logging()

CREDENTIALS = ['sender_name',
               'sender_email',
               'sender_address',
               'sender_phone']

REQUIRED_CREDENTIALS = ['sender_name',
                        'sender_email',
                        'sender_address']


async def invite_to_fill_credentials(chat_id, state):
    language = await get_ui_lang(state)
    text = locales.text(language, 'first_steps')

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    personal_info_button = types.InlineKeyboardButton(
        text=locales.text(language, 'send_personal_info'),
        callback_data='/enter_personal_info')

    settings_button = types.InlineKeyboardButton(
        text=locales.text(language, 'settings'),
        callback_data='/settings')

    keyboard.add(personal_info_button, settings_button)

    await bot.send_message(chat_id,
                           text,
                           reply_markup=keyboard)


async def invite_to_confirm_email(data, chat_id):
    language = await get_ui_lang(data=data)
    message = (locales.text(language, 'verify_email')).format(
                   data['sender_email']
                )

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    verify_email_button = types.InlineKeyboardButton(
        text=locales.text(language, 'verify_email_button'),
        callback_data='/verify_email')

    keyboard.add(verify_email_button)

    await bot.send_message(chat_id,
                           message,
                           reply_markup=keyboard,
                           parse_mode='HTML')


async def share_violation(state, username, chat_id):
    parameters = await prepare_mail_parameters(state)
    language = await get_ui_lang(state)

    try:
        mailer.send_mail(parameters)
        text = locales.text(language, 'letter_sent').format(config.CHANNEL)
        logger.info('Письмо отправлено - ' + str(username))

        async with state.proxy() as data:
            file = io.StringIO(parameters['html'])
            file.name = locales.text(language, 'letter_html')
            await bot.send_document(chat_id, file)

            caption = locales.text(language, 'violation_datetime') +\
                ' {}'.format(data['violation_datetime']) + '\n' +\
                locales.text(language, 'violation_location') +\
                ' {}'.format(data['violation_location']) + '\n' +\
                locales.text(language, 'violation_plate') + \
                ' {}'.format(data['vehicle_number'])

            # в канал
            await send_photos_group_with_caption(data,
                                                 config.CHANNEL,
                                                 caption)
    except Exception as exc:
        text = locales.text(language, 'sending_failed') + '\n' +\
            await humanize_message(exc, language)

        logger.error('Неудачка - ' + str(chat_id) + '\n' + str(exc))

    await bot.send_message(chat_id, text)


async def add_photo_to_attachments(photo, state):
    file = await bot.get_file(photo['file_id'])

    image_url = await uploader.get_permanent_url(
        config.URL_BASE + file.file_path)

    # потанцевально узкое место, все потоки всех пользователей будут ждать
    # пока кто-то один аппендит, если я правильно понимаю
    # нужно сделать каждому пользователю свой личный семафорчик, но я пока
    # что не знаю как
    async with semaphore, state.proxy() as data:
        if ('attachments' not in data) or ('photo_id' not in data):
            data['attachments'] = []
            data['photo_id'] = []

        data['attachments'].append(image_url)
        data['photo_id'].append(photo['file_id'])


async def delete_prepared_violation(data):
    # в этом месте сохраним адрес нарушения для использования в
    # следующем обращении
    data['previous_violation_address'] = data['violation_location']

    data['attachments'] = []
    data['photo_id'] = []
    data['vehicle_number'] = ''
    data['violation_location'] = ''
    data['violation_datetime'] = ''
    data['caption'] = ''


def set_default(data, key, value):
    if key not in data:
        data[key] = value


async def set_default_sender_info(data):
    for user_info in CREDENTIALS:
        if user_info not in data:
            data[user_info] = ''

    set_default(data, 'verified', False)
    data['secret_code'] = ''
    set_default(data, 'letter_lang', config.RU)
    set_default(data, 'ui_lang', config.BY)
    set_default(data, 'recipient', config.MINSK)
    set_default(data, 'previous_violation_address', '')
    data['saved_state'] = None

    data['attachments'] = []
    data['photo_id'] = []
    data['vehicle_number'] = ''
    data['violation_location'] = ''
    data['violation_datetime'] = ''


async def compose_summary(data):
    language = await get_ui_lang(data=data)

    text = locales.text(language, 'check_please').format(
            locales.text(language, data['recipient']),
            config.EMAIL_TO[data['recipient']]) + '\n' +\
        '\n' +\
        locales.text(language, 'letter_lang').format(
            locales.text(language, 'lang' + data['letter_lang'])) +\
        '\n' +\
        '\n' +\
        locales.text(language, 'sender') + '\n' +\
        locales.text(language, 'sender_name') +\
        ' <b>{}</b>'.format(data['sender_name']) + '\n' +\
        locales.text(language, 'sender_email') +\
        ' <b>{}</b>'.format(data['sender_email']) + '\n' +\
        locales.text(language, 'sender_address') +\
        ' <b>{}</b>'.format(data['sender_address']) + '\n' +\
        locales.text(language, 'sender_phone') +\
        ' <b>{}</b>'.format(data['sender_phone']) + '\n' +\
        '\n' +\
        locales.text(language, 'violator') + '\n' +\
        locales.text(language, 'violation_plate') +\
        ' <b>{}</b>'.format(data['vehicle_number']) + '\n' +\
        locales.text(language, 'violation_location') +\
        ' <b>{}</b>'.format(data['violation_location']) + '\n' +\
        locales.text(language, 'violation_datetime') +\
        ' <b>{}</b>'.format(data['violation_datetime']) + '\n' +\
        '\n' +\
        locales.text(language, 'channel_warning') + ' ' + config.CHANNEL

    return text


async def get_letter_header(data):
    template = path.join('letters',
                         'footer',
                         data['recipient'] + data['letter_lang'] + '.html')

    with open(template, 'r') as file:
        text = file.read()

    return text


async def get_letter_body(data):
    template = path.join('letters', 'body' + data['letter_lang'] + '.html')

    with open(template, 'r') as file:
        text = file.read()

    text = text.replace('__ГОСНОМЕРТС__', data['vehicle_number'])
    text = text.replace('__МЕСТОНАРУШЕНИЯ__', data['violation_location'])
    text = text.replace('__ДАТАИВРЕМЯ__', data['violation_datetime'])
    text = text.replace('__ИМЯЗАЯВИТЕЛЯ__', data['sender_name'])
    text = text.replace('__АДРЕСЗАЯВИТЕЛЯ__', data['sender_address'])
    text = text.replace('__ТЕЛЕФОНЗАЯВИТЕЛЯ__', data['sender_phone'])
    text = text.replace('__ПРИМЕЧАНИЕ__', data['caption'])

    return text


async def get_letter_photos(data):
    template = path.join('letters', 'photo.html')

    with open(template, 'r') as file:
        photo_template = file.read()

    text = ''

    for photo_url in data['attachments']:
        photo_link = photo_template.replace('__ССЫЛКА__', photo_url)
        photo = photo_link.replace('__ФОТОНАРУШЕНИЯ__', photo_url)
        text += photo

    return text


async def compose_letter_body(data):
    header = await get_letter_header(data)
    body = await get_letter_body(data)
    photos = await get_letter_photos(data)

    return header + body + photos


async def approve_sending(chat_id, state):
    language = await get_ui_lang(state)

    caption_button_text = locales.text(language, 'add_caption_button')

    async with state.proxy() as data:
        text = await compose_summary(data)
        await send_photos_group_with_caption(data, chat_id)

        if data['caption']:
            caption_button_text = locales.text(language,
                                               'change_caption_button')

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    approve_sending_button = types.InlineKeyboardButton(
        text=locales.text(language, 'approve_sending_button'),
        callback_data='/approve_sending')

    cancel_button = types.InlineKeyboardButton(
        text=locales.text(language, 'cancel_button'),
        callback_data='/cancel')

    enter_violation_info_button = types.InlineKeyboardButton(
        text=locales.text(language, 'violation_info_button'),
        callback_data='/enter_violation_info')

    add_caption_button = types.InlineKeyboardButton(
        text=caption_button_text,
        callback_data='/add_caption')

    keyboard.add(enter_violation_info_button, add_caption_button)
    keyboard.add(approve_sending_button, cancel_button)

    await bot.send_message(chat_id,
                           text,
                           reply_markup=keyboard,
                           parse_mode='HTML')


def get_subject(language):
    return locales.text(language, 'violation_letter')


async def prepare_mail_parameters(state):
    async with state.proxy() as data:
        recipient = locales.text(data['letter_lang'],
                                 'head_' + data['recipient'])

        parameters = {'to': {config.EMAIL_TO[data['recipient']]: recipient},
                      'from': [data['sender_email'], data['sender_name']],
                      'subject': get_subject(data['letter_lang']),
                      'html': await compose_letter_body(data),
                      'attachment': data['attachments']}

        return parameters


def get_str_current_time():
    tz_minsk = pytz.timezone('Europe/Minsk')
    current_time = datetime.now(tz_minsk)

    day = str(current_time.day).rjust(2, '0')
    month = str(current_time.month).rjust(2, '0')
    year = str(current_time.year)
    hour = str(current_time.hour).rjust(2, '0')
    minute = str(current_time.minute).rjust(2, '0')

    formatted_current_time = '{}.{}.{} {}:{}'.format(day,
                                                     month,
                                                     year,
                                                     hour,
                                                     minute)

    return formatted_current_time


async def invalid_credentials(state):
    async with state.proxy() as data:
        for user_info in REQUIRED_CREDENTIALS:
            if (user_info not in data) or (data[user_info] == ''):
                return True

    return False


async def verified_email(state):
    async with state.proxy() as data:
        if 'verified' not in data:
            data['verified'] = False
            return False

        return data['verified']


async def get_cancel_keyboard(data):
    language = await get_ui_lang(data=data)

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup()

    cancel = types.InlineKeyboardButton(
        text=locales.text(language, 'cancel_button'),
        callback_data='/cancel')

    keyboard.add(cancel)

    return keyboard


async def get_skip_keyboard(language):
    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    skip = types.InlineKeyboardButton(
        text=locales.text(language, 'skip_button'),
        callback_data='/skip')

    keyboard.add(skip)

    return keyboard


async def humanize_message(exception, language):
    invalid_email_msg = '\'message\': "valid \'from\' email address required"'
    invalid_email_humanized = locales.text(language, 'invalid_email')

    if invalid_email_msg in str(exception):
        return invalid_email_humanized

    return str(exception)


async def ask_for_user_address(chat_id, language):
    text = locales.text(language, 'input_sender_address') + '\n' +\
        locales.text(language, 'bot_can_guess_address') + '\n' +\
        '\n' +\
        locales.text(language, 'sender_address_example')

    keyboard = await get_skip_keyboard(language)

    await bot.send_message(chat_id,
                           text,
                           reply_markup=keyboard,
                           parse_mode='HTML')

    await Form.sender_address.set()


async def ask_for_user_email(chat_id, language):
    text = locales.text(language, 'input_email') + '\n' +\
        locales.text(language, 'nonexistent_email_warning') + '\n' +\
        '\n' +\
        locales.text(language, 'email_example')

    keyboard = await get_skip_keyboard(language)

    await bot.send_message(chat_id,
                           text,
                           reply_markup=keyboard,
                           parse_mode='HTML')

    await Form.sender_email.set()


async def ask_for_user_phone(chat_id, language):
    text = locales.text(language, 'input_phone') + '\n' +\
        '\n' +\
        locales.text(language, 'phone_example')

    keyboard = await get_skip_keyboard(language)

    await bot.send_message(chat_id,
                           text,
                           reply_markup=keyboard,
                           parse_mode='HTML')

    await Form.sender_phone.set()


async def show_private_info_summary(chat_id, state):
    language = await get_ui_lang(state)

    if await invalid_credentials(state):
        text = locales.text(language, 'no_info_warning')
        await bot.send_message(chat_id, text)
    elif not await verified_email(state):
        async with state.proxy() as data:
            await invite_to_confirm_email(data, chat_id)
    else:
        text = locales.text(language, 'ready_to_report')
        await bot.send_message(chat_id, text)

    await Form.operational_mode.set()


async def ask_for_violation_address(chat_id, data):
    language = await get_ui_lang(data=data)

    text = locales.text(language, 'input_violation_address') + '\n' +\
        locales.text(language, 'bot_can_guess_address') + '\n' +\
        '\n' +\
        locales.text(language, 'violation_address_example') + '\n' +\
        '\n'

    # настроим клавиатуру
    keyboard = await get_cancel_keyboard(data)

    if 'previous_violation_address' in data:
        if data['previous_violation_address'] != '':
            text += locales.text(language, 'previous_violation_address') +\
                ' <b>{}</b>'.format(data['previous_violation_address'])

            use_previous_button = types.InlineKeyboardButton(
                text=locales.text(language, 'use_previous_button'),
                callback_data='/use_previous')

            keyboard.add(use_previous_button)

    await bot.send_message(chat_id,
                           text,
                           reply_markup=keyboard,
                           parse_mode='HTML')

    await Form.violation_location.set()


async def send_language_info(chat_id, data):
    text, keyboard = await get_language_text_and_keyboard(data)

    await bot.send_message(chat_id,
                           text,
                           reply_markup=keyboard,
                           parse_mode='HTML')


async def save_recipient(region, data):
    if region is None:
        data['recipient'] = config.MINSK
    else:
        data['recipient'] = region


async def print_violation_address_info(region, address, chat_id, language):
    text = locales.text(language, 'recipient') +\
        ' <b>{}</b>.'.format(locales.text(language, region)) + '\n' +\
        '\n' +\
        locales.text(language, 'violation_address') + \
        ' <b>{}</b>'.format(address)

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    enter_violation_addr_button = types.InlineKeyboardButton(
        text=locales.text(language, 'change_violation_addr_button'),
        callback_data='/enter_violation_addr')

    enter_recipient_button = types.InlineKeyboardButton(
        text=locales.text(language, 'change_recipient'),
        callback_data='/enter_recipient')

    keyboard.add(enter_violation_addr_button, enter_recipient_button)

    await bot.send_message(chat_id,
                           text,
                           reply_markup=keyboard,
                           parse_mode='HTML')


async def save_violation_address(address, data):
    data['violation_location'] = address

async def ask_for_violation_time(chat_id, language):
    current_time = get_str_current_time()

    text = locales.text(language, 'input_datetime') + '\n' +\
        '\n' +\
        locales.text(language, 'example') + \
        ' <b>{}</b>.'.format(current_time)

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    current_time_button = types.InlineKeyboardButton(
        text=locales.text(language, 'current_time_button'),
        callback_data='/current_time')

    cancel = types.InlineKeyboardButton(
        text=locales.text(language, 'cancel_button'),
        callback_data='/cancel')

    keyboard.add(current_time_button, cancel)

    await bot.send_message(chat_id,
                           text,
                           reply_markup=keyboard,
                           parse_mode='HTML')

    await Form.violation_datetime.set()


async def send_photos_group_with_caption(data, chat_id, caption=''):
    photos_id = data['photo_id']

    photos = []

    for count, photo_id in enumerate(photos_id):
        text = ''

        # первой фотке добавим общее описание
        if count == 0:
            text = caption

        photo = PhotoItem('photo', photo_id, text)
        photos.append(photo)

    await bot.send_media_group(chat_id=chat_id, media=photos)


def prepare_registration_number(number: str):
    '''заменяем в номере все символы на киррилические'''

    kyrillic = 'ABCEHKMOPTXYІ'
    latin = 'ABCEHKMOPTXYI'

    up_number = number.upper().strip()

    for num, symbol in enumerate(latin):
        up_number = up_number.replace(symbol, kyrillic[num])

    return up_number


async def set_violation_location(chat_id, address, state):
    coordinates = await locator.get_coordinates(address)
    region = await locator.get_region(coordinates)

    async with state.proxy() as data:
        await save_violation_address(address, data)
        await save_recipient(region, data)
        region = data['recipient']
        language = await get_ui_lang(data=data)

    await print_violation_address_info(region,
                                       address,
                                       chat_id,
                                       language)

    await ask_for_violation_time(chat_id,
                                 language)


async def show_settings(message, state):
    logger.info('Настройки - ' + str(message.from_user.username))

    async with state.proxy() as data:
        language = await get_ui_lang(data=data)

    text = locales.text(language, 'select_section')

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    personal_info_button = types.InlineKeyboardButton(
        text=locales.text(language, 'personal_info'),
        callback_data='/personal_info')

    language_settings_button = types.InlineKeyboardButton(
        text=locales.text(language, 'language_settings'),
        callback_data='/language_settings')

    keyboard.add(personal_info_button, language_settings_button)

    await bot.send_message(message.chat.id,
                           text,
                           reply_markup=keyboard,
                           parse_mode='HTML')


async def enter_personal_info(message, state):
    logger.info('Настройка отправителя - ' + str(message.from_user.username))

    async with state.proxy() as data:
        await set_default_sender_info(data)
        language = await get_ui_lang(data=data)

    text = locales.text(language, 'input_fullname') + '\n' +\
        '\n' +\
        locales.text(language, 'fullname_example')

    keyboard = await get_skip_keyboard(language)

    await bot.send_message(message.chat.id,
                           text,
                           reply_markup=keyboard,
                           parse_mode='HTML')

    await Form.sender_name.set()


async def get_ui_lang_or_default(data):
    try:
        return data['ui_lang']
    except KeyError:
        set_default(data, 'ui_lang', config.RU)
        return data['ui_lang']


async def get_ui_lang(state=None, data=None):
    if data:
        return await get_ui_lang_or_default(data)
    elif state:
        async with state.proxy() as my_data:
            return await get_ui_lang_or_default(my_data)


async def show_personal_info(message: types.Message, state: FSMContext):
    logger.info('Показ инфы отправителя - ' + str(message.from_user.username))

    async with state.proxy() as data:
        language = await get_ui_lang(data=data)

        text = locales.text(language, 'personal_data') + '\n' + '\n' +\
            locales.text(language, 'sender_name') +\
            ' <b>{}</b>'.format(data['sender_name']) + '\n' +\
            locales.text(language, 'sender_email') +\
            ' <b>{}</b>'.format(data['sender_email']) + '\n' +\
            locales.text(language, 'sender_address') +\
            ' <b>{}</b>'.format(data['sender_address']) + '\n' +\
            locales.text(language, 'sender_phone') + \
            ' <b>{}</b>'.format(data['sender_phone']) + '\n'

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    enter_personal_info_button = types.InlineKeyboardButton(
        text=locales.text(language, 'enter_personal_info_button'),
        callback_data='/enter_personal_info')

    delete_personal_info_button = types.InlineKeyboardButton(
        text=locales.text(language, 'delete_personal_info_button'),
        callback_data='/reset')

    keyboard.add(enter_personal_info_button, delete_personal_info_button)

    await bot.send_message(message.chat.id,
                           text,
                           reply_markup=keyboard,
                           parse_mode='HTML')


async def get_language_text_and_keyboard(data):
    language = await get_ui_lang(data=data)

    if 'letter_lang' not in data:
        data['letter_lang'] = config.RU

    ui_lang_name = locales.text(language, 'lang' + language)
    letter_lang_name = locales.text(language, 'lang' + data['letter_lang'])

    text = locales.text(language, 'current_ui_lang') +\
        ' <b>{}</b>.'.format(ui_lang_name) + '\n' +\
        '\n' +\
        locales.text(language, 'current_letter_lang') +\
        ' <b>{}</b>.'.format(letter_lang_name)

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    change_ui_language_button = types.InlineKeyboardButton(
        text=locales.text(language, 'change_ui_language_button'),
        callback_data='/change_ui_language')

    change_letter_language_button = types.InlineKeyboardButton(
        text=locales.text(language, 'change_letter_language_button'),
        callback_data='/change_letter_language')

    keyboard.add(change_ui_language_button, change_letter_language_button)

    return text, keyboard


async def user_banned(*args):
    bot_id = (await bot.get_me()).id

    async with dp.current_state(chat=bot_id, user=bot_id).proxy() as data:
        for name in args:
            if name in data['banned_users']:
                return True, data['banned_users'][name]

    return False, ''


@dp.callback_query_handler(lambda call: call.data == '/settings',
                           state='*')
async def settings_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки настроек - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)
    await show_settings(call.message, state)


@dp.callback_query_handler(lambda call: call.data == '/personal_info',
                           state='*')
async def personal_info_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки показа личных данных - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)
    await show_personal_info(call.message, state)


@dp.callback_query_handler(lambda call: call.data == '/language_settings',
                           state='*')
async def language_settings_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки языковых настроек - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)

    async with state.proxy() as data:
        await send_language_info(call.message.chat.id, data)


@dp.callback_query_handler(lambda call: call.data == '/enter_personal_info',
                           state='*')
async def enter_personal_info_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки ввода личных данных - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)
    await enter_personal_info(call.message, state)


@dp.callback_query_handler(lambda call: call.data == '/verify_email',
                           state='*')
async def verify_email_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки верификации почты - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)
    language = await get_ui_lang(state)

    if await verified_email(state):
        text = locales.text(language, 'email_already_verified')
        await bot.send_message(call.message.chat.id, text)
        return

    async with state.proxy() as data:
        secret_code = await mail_verifier.verify(data['sender_email'])

    if secret_code == config.VERIFYING_FAIL:
        text = locales.text(language, 'email_verifying_fail')

        await Form.operational_mode.set()
    else:
        text = locales.text(language, 'enter_secret_code') + '\n' +\
            locales.text(language, 'spam_folder')

        async with state.proxy() as data:
            data['secret_code'] = secret_code

        await Form.email_verifying.set()

    await bot.send_message(call.message.chat.id, text)


@dp.callback_query_handler(lambda call: call.data == '/reset',
                           state='*')
async def delete_personal_info_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки удаления личных данных - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)
    await cmd_reset(call.message, state)


@dp.callback_query_handler(lambda call: call.data == '/skip',
                           state=Form.sender_name)
async def skip_name_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки пропуска ввода ФИО - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)
    await ask_for_user_email(call.message.chat.id,
                             await get_ui_lang(state))


@dp.callback_query_handler(lambda call: call.data == '/use_previous',
                           state=Form.violation_location)
async def use_previous_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие предыдущий адрес - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)

    async with state.proxy() as data:
        previous_address = data['previous_violation_address']

    await set_violation_location(call.message.chat.id,
                                 previous_address,
                                 state)


@dp.callback_query_handler(lambda call: call.data == '/change_ui_language',
                           state='*')
async def change_language_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки смены языка бота - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)

    async with state.proxy() as data:
        if await get_ui_lang(data=data) == config.RU:
            data['ui_lang'] = config.BY
        elif data['ui_lang'] == config.BY:
            data['ui_lang'] = config.RU
        else:
            data['ui_lang'] = config.RU

        text, keyboard = await get_language_text_and_keyboard(data)

    await bot.edit_message_text(text,
                                call.message.chat.id,
                                call.message.message_id,
                                reply_markup=keyboard,
                                parse_mode='HTML')


@dp.callback_query_handler(lambda call: call.data == '/change_letter_language',
                           state='*')
async def change_language_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки смены языка писем - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)

    async with state.proxy() as data:
        if data['letter_lang'] == config.RU:
            data['letter_lang'] = config.BY
        elif data['letter_lang'] == config.BY:
            data['letter_lang'] = config.RU
        else:
            data['letter_lang'] = config.RU

        text, keyboard = await get_language_text_and_keyboard(data)

    await bot.edit_message_text(text,
                                call.message.chat.id,
                                call.message.message_id,
                                reply_markup=keyboard,
                                parse_mode='HTML')


@dp.callback_query_handler(lambda call: call.data == '/skip',
                           state=Form.sender_email)
async def skip_email_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки пропуска ввода email - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)
    await ask_for_user_address(call.message.chat.id,
                               await get_ui_lang(state))


@dp.callback_query_handler(lambda call: call.data == '/skip',
                           state=Form.sender_address)
async def skip_address_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки пропуска ввода адреса - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)

    await ask_for_user_phone(call.message.chat.id,
                             await get_ui_lang(state))


@dp.callback_query_handler(lambda call: call.data == '/skip',
                           state=Form.sender_phone)
async def skip_phone_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки пропуска ввода телефона - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)
    await show_private_info_summary(call.message.chat.id, state)


@dp.callback_query_handler(lambda call: call.data == '/current_time',
                           state=Form.violation_datetime)
async def current_time_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки ввода текущего времени - ' +
                str(call.from_user.username))

    current_time = get_str_current_time()

    message = await bot.send_message(call.message.chat.id, current_time)
    await catch_violation_time(message, state)


@dp.callback_query_handler(lambda call: call.data == '/enter_sender_address',
                           state=Form.sender_phone)
async def sender_address_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки ввода своего адреса - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)
    await ask_for_user_address(call.message.chat.id,
                               await get_ui_lang(state))


@dp.callback_query_handler(lambda call: call.data == '/enter_violation_addr',
                           state=Form.violation_datetime)
async def violation_address_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки ввода адреса нарушения - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)

    async with state.proxy() as data:
        await ask_for_violation_address(call.message.chat.id, data)


@dp.callback_query_handler(lambda call: call.data == '/enter_recipient',
                           state=Form.violation_datetime)
async def recipient_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки ввода реципиента - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)
    language = await get_ui_lang(state)

    # этот текст не менять или менять по всему файлу
    text = locales.text(language, 'choose_recipient')

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    for region in config.REGIONS:
        button = types.InlineKeyboardButton(
            text=locales.text(language, region),
            callback_data=region)

        keyboard.add(button)

    await bot.send_message(call.message.chat.id,
                           text,
                           reply_markup=keyboard)

    await Form.recipient.set()


@dp.callback_query_handler(
    lambda call: locales.text_exists('choose_recipient', call.message.text),
    state=Form.recipient)
async def recipient_choosen_click(call, state: FSMContext):
    logger.info('Выбрал реципиента - ' + str(call.from_user.username))

    await bot.answer_callback_query(call.id)

    async with state.proxy() as data:
        address = data['violation_location']
        await save_recipient(call.data, data)
        region = data['recipient']

    language = await get_ui_lang(state)

    await print_violation_address_info(region,
                                       address,
                                       call.message.chat.id,
                                       language)

    await ask_for_violation_time(call.message.chat.id, language)


@dp.callback_query_handler(lambda call: call.data == '/enter_violation_info',
                           state=[Form.violation_photo,
                                  Form.violation_sending])
async def enter_violation_info_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки ввода инфы о нарушении - ' +
                str(call.from_user.username))

    async with state.proxy() as data:
        language = await get_ui_lang(data=data)

        # зададим сразу пустое примечание
        data['caption'] = ''

    text = locales.text(language, 'input_plate') + '\n' +\
        '\n' +\
        locales.text(language, 'plate_example')

    # настроим клавиатуру
    async with state.proxy() as data:
        keyboard = await get_cancel_keyboard(data)

    await bot.answer_callback_query(call.id)

    await bot.send_message(call.message.chat.id,
                           text,
                           reply_markup=keyboard,
                           parse_mode='HTML')

    await Form.vehicle_number.set()


@dp.callback_query_handler(lambda call: call.data == '/add_caption',
                           state=[Form.violation_sending])
async def add_caption_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки ввода примечания - ' +
                str(call.from_user.username))

    async with state.proxy() as data:
        # зададим сразу пустое примечание
        data['caption'] = ''

        # сохраним состояние, чтобы к нему вернуться
        current_state = await state.get_state()
        data['saved_state'] = current_state

        language = await get_ui_lang(data=data)

    text = locales.text(language, 'input_caption')

    # настроим клавиатуру
    async with state.proxy() as data:
        keyboard = await get_cancel_keyboard(data)

    await bot.answer_callback_query(call.id)
    await bot.send_message(call.message.chat.id, text, reply_markup=keyboard)
    await Form.caption.set()


@dp.callback_query_handler(lambda call: call.data == '/answer_feedback',
                           state='*')
async def answer_feedback_click(call, state: FSMContext):
    logger.info('Обрабатываем нажатие кнопки ответа на фидбэк - ' +
                str(call.from_user.username))

    async with state.proxy() as data:
        # сохраняем текущее состояние
        current_state = await state.get_state()

        if current_state != Form.feedback_answering.state:
            data['saved_state'] = current_state

        # сохраняем адресата
        data['feedback_post'] = call.message.text

        language = await get_ui_lang(data=data)
        text = locales.text(language, 'input_reply')

        # настроим клавиатуру
        keyboard = await get_cancel_keyboard(data)

    await bot.answer_callback_query(call.id)

    await bot.send_message(call.message.chat.id,
                           text,
                           reply_markup=keyboard,
                           reply_to_message_id=call.message.message_id)

    await Form.feedback_answering.set()


@dp.callback_query_handler(lambda call: call.data == '/cancel',
                           state=[Form.violation_photo,
                                  Form.vehicle_number,
                                  Form.violation_datetime,
                                  Form.violation_location,
                                  Form.violation_sending,
                                  Form.feedback,
                                  Form.feedback_answering,
                                  Form.caption])
async def cancel_violation_input(call, state: FSMContext):
    logger.info('Отмена, возврат в рабочий режим - ' +
                str(call.from_user.username))

    await bot.answer_callback_query(call.id)

    async with state.proxy() as data:
        language = await get_ui_lang(data=data)

        if 'saved_state' in data:
            if data['saved_state'] is not None:
                saved_state = data['saved_state']
                await state.set_state(saved_state)
                data['saved_state'] = None

                text = locales.text(language, 'continue_work')
                await bot.send_message(call.message.chat.id, text)
                return

        await delete_prepared_violation(data)
        data['feedback_post'] = ''

    text = locales.text(language, 'operation_mode')
    await bot.send_message(call.message.chat.id, text)
    await Form.operational_mode.set()


@dp.callback_query_handler(lambda call: call.data == '/approve_sending',
                           state=Form.violation_sending)
async def send_letter_click(call, state: FSMContext):
    logger.info('Отправляем письмо в ГАИ - ' +
                str(call.from_user.username))

    language = await get_ui_lang(state)

    if await invalid_credentials(state):
        text = locales.text(language, 'need_personal_info')

        logger.info('Письмо не отправлено, не введены личные данные - ' +
                    str(call.from_user.username))

        await bot.send_message(call.message.chat.id, text)
    elif not await verified_email(state):
        logger.info('Письмо не отправлено, email не подтвержден - ' +
                    str(call.from_user.username))

        async with state.proxy() as data:
            await invite_to_confirm_email(data, call.message.chat.id)
    else:
        await share_violation(state,
                              call.from_user.username,
                              call.message.chat.id)

    # из-за того, что письмо может отправляться долго,
    # телеграм может погасить кружочек ожидания сам, и тогда будет исключение
    try:
        await bot.answer_callback_query(call.id)
    except InvalidQueryID:
        pass

    async with state.proxy() as data:
        await delete_prepared_violation(data)

    await Form.operational_mode.set()


@dp.callback_query_handler(state='*')
async def reject_button_click(call, state: FSMContext):
    logger.info('Беспорядочно кликает на кнопки - ' +
                str(call.from_user.username))

    language = await get_ui_lang(state)

    text = locales.text(language, 'irrelevant_action')

    await bot.answer_callback_query(call.id)
    await bot.send_message(call.message.chat.id, text)


@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message, state: FSMContext):
    """
    Conversation's entry point
    """
    logger.info('Старт работы бота - ' + str(message.from_user.username))

    language = await get_ui_lang(state)
    text = locales.text(language, 'greeting')

    await bot.send_message(message.chat.id,
                           text)

    await Form.initial.set()

    async with state.proxy() as data:
        await set_default_sender_info(data)

    await invite_to_fill_credentials(message.chat.id, state)


@dp.message_handler(commands=['settings'], state='*')
async def show_settings_command(message: types.Message, state: FSMContext):
    logger.info('Показ настроек команда - ' + str(message.from_user.username))
    await show_settings(message, state)


@dp.message_handler(commands=['banlist'], state='*')
async def banlist_user_command(message: types.Message, state: FSMContext):
    if message.chat.id != config.ADMIN_ID:
        return

    logger.info('Банлист - ' + str(message.from_user.username))

    bot_id = (await bot.get_me()).id

    async with dp.current_state(chat=bot_id, user=bot_id).proxy() as data:
        text = str(data['banned_users'])
        await bot.send_message(message.chat.id, text)


@dp.message_handler(commands=['unban'], state='*')
async def unban_user_command(message: types.Message, state: FSMContext):
    if message.chat.id != config.ADMIN_ID:
        return

    language = await get_ui_lang(state)
    logger.info('Забанил человека - ' + str(message.from_user.username))

    user = message.text.replace('/unban', '', 1).strip()

    if not user:
        text = locales.text(language, 'banned_name_expected')
        await bot.send_message(message.chat.id, text)
        return

    bot_id = (await bot.get_me()).id

    async with dp.current_state(chat=bot_id, user=bot_id).proxy() as data:
        data['banned_users'].pop(user, None)
        text = user + ' ' + locales.text(language, 'unbanned_succesfully')

    await bot.send_message(message.chat.id, text)


@dp.message_handler(commands=['ban'], state='*')
async def ban_user_command(message: types.Message, state: FSMContext):
    if message.chat.id != config.ADMIN_ID:
        return

    language = await get_ui_lang(state)
    logger.info('Забанил человека - ' + str(message.from_user.username))

    try:
        user, caption = message.text.replace('/ban ', '', 1).split(' ', 1)
    except ValueError:
        text = locales.text(language, 'name_and_caption_expected')
        await bot.send_message(message.chat.id, text)
        return

    bot_id = (await bot.get_me()).id

    async with dp.current_state(chat=bot_id, user=bot_id).proxy() as data:
        try:
            data['banned_users'][user] = caption
        except KeyError:
            data['banned_users'] = {}
            data['banned_users'][user] = caption

        text = user + ' ' + locales.text(language, 'banned_succesfully')

    await bot.send_message(message.chat.id, text)


@dp.message_handler(commands=['reset'], state='*')
async def cmd_reset(message: types.Message, state: FSMContext):
    logger.info('Сброс бота - ' + str(message.from_user.username))
    language = await get_ui_lang(state)

    await state.finish()
    await Form.initial.set()

    text = locales.text(language, 'reset') + ' ¯\_(ツ)_/¯'
    await bot.send_message(message.chat.id, text)

    async with state.proxy() as data:
        await set_default_sender_info(data)

    await invite_to_fill_credentials(message.chat.id, state)


@dp.message_handler(commands=['help'], state='*')
async def cmd_help(message: types.Message, state: FSMContext):
    logger.info('Вызов помощи - ' + str(message.from_user.username))

    language = await get_ui_lang(state)

    text = locales.text(language, 'first_step_help') + '\n' +\
        '\n' +\
        locales.text(language, 'language_help') + '\n' +\
        '\n' +\
        locales.text(language, 'address_help') + '\n' +\
        '\n' +\
        locales.text(language, 'plate_help') + '\n' +\
        '\n' +\
        locales.text(language, 'feel_free_to_try_help') + '\n' +\
        '\n' +\
        locales.text(language, 'before_sending_help') + '\n' +\
        '\n' +\
        locales.text(language, 'channel_help').format(
            config.CHANNEL) + '\n' +\
        '\n' +\
        locales.text(language, 'copy_of_letter_help') + '\n' +\
        '\n' +\
        locales.text(language, 'feedback_help')

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    privacy_policy = types.InlineKeyboardButton(
        text=locales.text(language, 'privacy_policy_button'),
        url='https://telegra.ph/Politika-konfidencialnosti-01-09')

    letter_template = types.InlineKeyboardButton(
        text=locales.text(language, 'letter_template_button'),
        url='https://docs.google.com/document/d/' +
            '11kigeRPEdqbYcMcFVmg1lv66Fy-eOyf5i1PIQpSqcII/edit?usp=sharing')

    changelog = types.InlineKeyboardButton(
        text='Changelog',
        url='https://github.com/dziaineka/parkun-bot/blob/master/README.md')

    keyboard.add(privacy_policy, letter_template, changelog)

    await bot.send_message(message.chat.id, text, reply_markup=keyboard)


@dp.message_handler(commands=['feedback'], state='*')
async def write_feedback(message: types.Message, state: FSMContext):
    logger.info('Хочет написать фидбэк - ' + str(message.from_user.username))

    async with state.proxy() as data:
        current_state = await state.get_state()

        if current_state != Form.feedback.state:
            data['saved_state'] = current_state

        language = await get_ui_lang(data=data)
        text = locales.text(language, 'input_feedback')

        keyboard = await get_cancel_keyboard(data)

    await bot.send_message(message.chat.id, text, reply_markup=keyboard)
    await Form.feedback.set()


@dp.message_handler(state=Form.feedback)
async def catch_feedback(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем ввод фидбэка - ' +
                str(message.from_user.username))

    language = await get_ui_lang(state)

    await bot.forward_message(
        chat_id=config.ADMIN_ID,
        from_chat_id=message.from_user.id,
        message_id=message.message_id,
        disable_notification=True)

    text = str(message.from_user.id) + ' ' + str(message.message_id)

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    give_feedback_button = types.InlineKeyboardButton(
        text=locales.text(language, 'reply_button'),
        callback_data='/answer_feedback')

    keyboard.add(give_feedback_button)

    await bot.send_message(config.ADMIN_ID, text, reply_markup=keyboard)

    text = locales.text(language, 'thanks_for_feedback')
    await bot.send_message(message.chat.id, text)

    async with state.proxy() as data:
        saved_state = data['saved_state']
        await state.set_state(saved_state)
        data['saved_state'] = None


@dp.message_handler(content_types=types.ContentType.TEXT,
                    state=Form.feedback_answering)
async def catch_sender_name(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем ответ на фидбэк - ' +
                str(message.from_user.username))

    async with state.proxy() as data:
        feedback = data['feedback_post'].split(' ')
        feedback_chat_id = feedback[0]
        feedback_message_id = feedback[1]

        await bot.send_message(feedback_chat_id,
                               message.text,
                               reply_to_message_id=feedback_message_id)

        await state.set_state(data['saved_state'])
        data['saved_state'] = None

        language = await get_ui_lang(data=data)

    text = locales.text(language, 'continue_work')
    await bot.send_message(message.chat.id, text)


@dp.message_handler(content_types=types.ContentType.TEXT,
                    state=Form.email_verifying)
async def catch_secret_code(message: types.Message, state: FSMContext):
    logger.info('Ввод секретного кода - ' + str(message.from_user.username))

    async with state.proxy() as data:
        secret_code = data['secret_code']
        language = await get_ui_lang(data=data)

    if secret_code == message.text:
        async with state.proxy() as data:
            data['verified'] = True

        text = locales.text(language, 'email_verified')
    else:
        text = locales.text(language, 'reply_verification') + '\n' +\
            locales.text(language, 'press_feedback')

    await bot.send_message(message.chat.id, text)
    await Form.operational_mode.set()


@dp.message_handler(content_types=types.ContentType.TEXT,
                    state=Form.sender_name)
async def catch_sender_name(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем ввод ФИО - ' + str(message.from_user.username))

    async with state.proxy() as data:
        data['sender_name'] = message.text

    await ask_for_user_email(message.chat.id,
                             await get_ui_lang(state))


@dp.message_handler(content_types=types.ContentType.TEXT,
                    state=Form.sender_email)
async def catch_sender_email(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем ввод email - ' + str(message.from_user.username))
    language = await get_ui_lang(state)

    try:
        if message.text.split('@')[1] in blocklist:
            logger.info('Временный email - ' + str(message.from_user.username))
            text = locales.text(language, 'no_temporary_email')
            await bot.send_message(message.chat.id, text)

            await ask_for_user_email(message.chat.id,
                                     language)

            return
    except IndexError:
        pass

    async with state.proxy() as data:
        data['sender_email'] = message.text
        data['verified'] = False
        await ask_for_user_address(message.chat.id,
                                   language)


@dp.message_handler(content_types=types.ContentType.TEXT,
                    state=Form.sender_address)
async def catch_sender_address(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем ввод адреса - ' +
                str(message.from_user.username))

    async with state.proxy() as data:
        data['sender_address'] = message.text

    await ask_for_user_phone(message.chat.id,
                             await get_ui_lang(state))


@dp.message_handler(content_types=types.ContentType.LOCATION,
                    state=Form.sender_address)
async def catch_gps_sender_address(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем ввод адреса по локации - ' +
                str(message.from_user.username))

    coordinates = (str(message.location.longitude) + ', ' +
                   str(message.location.latitude))

    async with state.proxy() as data:
        language = await get_ui_lang(data=data)
        address = await locator.get_address(coordinates, data['letter_lang'])

        if address == config.ADDRESS_FAIL:
            address = locales.text(language, 'no_address_detected')

    if address is None:
        logger.info('Не распознал локацию - ' +
                    str(message.from_user.username))

        text = locales.text(language, 'cant_locate')
        await bot.send_message(message.chat.id, text)
        return

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    enter_sender_address = types.InlineKeyboardButton(
        text=locales.text(language, 'change_violation_addr_button'),
        callback_data='/enter_sender_address')

    keyboard.add(enter_sender_address)

    bot_message = await bot.send_message(message.chat.id,
                                         address,
                                         reply_markup=keyboard)

    await catch_sender_address(bot_message, state)


@dp.message_handler(content_types=types.ContentType.TEXT,
                    state=Form.sender_phone)
async def catch_sender_phone(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем ввод телефона - ' +
                str(message.from_user.username))

    async with state.proxy() as data:
        data['sender_phone'] = message.text

    await show_private_info_summary(message.chat.id, state)


@dp.message_handler(content_types=types.ContentTypes.PHOTO,
                    state=[Form.operational_mode,
                           Form.violation_photo])
async def process_violation_photo(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем посылку фотки нарушения - ' +
                str(message.from_user.username))

    language = await get_ui_lang(state)

    banned, reason = await user_banned(message.from_user.username,
                                       str(message.chat.id))

    if banned:
        text = locales.text(language, 'you_are_banned') + ' ' + reason

        await bot.send_message(message.chat.id, text)
        return

    # Добавляем фотку наилучшего качества(последнюю в массиве) в список
    # прикрепления в письме
    asyncio.run_coroutine_threadsafe(
        add_photo_to_attachments(message.photo[-1], state), loop)

    text = locales.text(language, 'photo_or_info') + '\n' +\
        '\n' +\
        '👮🏻‍♂️' + ' ' + locales.text(language, 'photo_quality_warning')

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    enter_violation_info = types.InlineKeyboardButton(
        text=locales.text(language, 'violation_info_button'),
        callback_data='/enter_violation_info')

    cancel = types.InlineKeyboardButton(
        text=locales.text(language, 'cancel_button'),
        callback_data='/cancel')

    keyboard.add(enter_violation_info, cancel)

    await message.reply(text, reply_markup=keyboard, parse_mode='HTML')
    await Form.violation_photo.set()


@dp.message_handler(content_types=types.ContentType.TEXT,
                    state=Form.vehicle_number)
async def catch_vehicle_number(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем ввод гос. номера - ' +
                str(message.from_user.username))

    async with state.proxy() as data:
        data['vehicle_number'] = prepare_registration_number(message.text)
        await ask_for_violation_address(message.chat.id, data)


@dp.message_handler(content_types=types.ContentType.TEXT,
                    state=Form.caption)
async def catch_vehicle_number(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем ввод примечания - ' +
                str(message.from_user.username))

    async with state.proxy() as data:
        data['saved_state'] = None
        data['caption'] = message.text.strip()

    await Form.violation_sending.set()
    await approve_sending(message.chat.id, state)


@dp.message_handler(content_types=types.ContentType.ANY,
                    state=Form.caption)
async def catch_vehicle_number(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем ввод неправильного примечания - ' +
                str(message.from_user.username))

    language = await get_ui_lang(state)

    text = locales.text(language, 'text_only')
    await bot.send_message(message.chat.id, text)


@dp.message_handler(content_types=types.ContentType.TEXT,
                    state=Form.violation_location)
async def catch_violation_location(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем ввод адреса нарушения - ' +
                str(message.from_user.username))

    await set_violation_location(message.chat.id, message.text, state)


@dp.message_handler(content_types=types.ContentType.LOCATION,
                    state=Form.violation_location)
async def catch_gps_violation_location(message: types.Message,
                                       state: FSMContext):
    logger.info('Обрабатываем ввод локации адреса нарушения - ' +
                str(message.from_user.username))

    coordinates = [message.location.longitude, message.location.latitude]

    async with state.proxy() as data:
        language = await get_ui_lang(data=data)
        address = await locator.get_address(coordinates, data['letter_lang'])

        if address == config.ADDRESS_FAIL:
            address = locales.text(language, 'no_address_detected')

        region = await locator.get_region(coordinates)
        await save_recipient(region, data)
        region = data['recipient']

    if address is None:
        logger.info('Не распознал локацию - ' +
                    str(message.from_user.username))

        text = locales.text(language, 'cant_locate')
        await bot.send_message(message.chat.id, text)
        return

    async with state.proxy() as data:
        await save_violation_address(address, data)

    await print_violation_address_info(region,
                                       address,
                                       message.chat.id,
                                       language)

    await ask_for_violation_time(message.chat.id, language)


@dp.message_handler(content_types=types.ContentType.TEXT,
                    state=Form.violation_datetime)
async def catch_violation_time(message: types.Message, state: FSMContext):
    logger.info('Обрабатываем ввод даты и времени нарушения - ' +
                str(message.chat.username))

    async with state.proxy() as data:
        data['violation_datetime'] = message.text

    await Form.violation_sending.set()
    await approve_sending(message.chat.id, state)


@dp.message_handler(content_types=types.ContentTypes.ANY, state=Form.initial)
async def ignore_initial_input(message: types.Message, state: FSMContext):
    await invite_to_fill_credentials(message.chat.id, state)


@dp.message_handler(content_types=types.ContentTypes.ANY,
                    state=Form.operational_mode)
async def reject_wrong_input(message: types.Message, state: FSMContext):
    logger.info('Посылает не фотку, а что-то другое - ' +
                str(message.from_user.username))

    language = await get_ui_lang(state)
    text = locales.text(language, 'great_expectations')

    await bot.send_message(message.chat.id, text)


@dp.message_handler(content_types=types.ContentTypes.ANY,
                    state=Form.violation_photo)
async def reject_wrong_violation_photo_input(message: types.Message,
                                             state: FSMContext):
    language = await get_ui_lang(state)
    text = locales.text(language, 'photo_or_info')

    # настроим клавиатуру
    keyboard = types.InlineKeyboardMarkup(row_width=2)

    enter_violation_info = types.InlineKeyboardButton(
        text=locales.text(language, 'violation_info_button'),
        callback_data='/enter_violation_info')

    cancel = types.InlineKeyboardButton(
        text=locales.text(language, 'cancel_button'),
        callback_data='/cancel')

    keyboard.add(enter_violation_info, cancel)

    await bot.send_message(message.chat.id, text, reply_markup=keyboard)


@dp.message_handler(content_types=types.ContentTypes.ANY,
                    state=[Form.vehicle_number,
                           Form.violation_datetime,
                           Form.violation_location])
async def reject_wrong_violation_data_input(message: types.Message,
                                            state: FSMContext):
    language = await get_ui_lang(state)
    text = locales.text(language, 'text_only')

    await bot.send_message(message.chat.id, text)


async def startup(dispatcher: Dispatcher):
    logger.info('Старт бота.')
    logger.info('Загружаем границы регионов.')
    await locator.download_boundaries()
    logger.info('Загрузили.')


async def shutdown(dispatcher: Dispatcher):
    logger.info('Убиваем бота.')

    await dispatcher.storage.close()
    await dispatcher.storage.wait_closed()


def main():
    executor.start_polling(dp,
                           loop=loop,
                           skip_updates=True,
                           on_startup=startup,
                           on_shutdown=shutdown)


if __name__ == '__main__':
    main()
