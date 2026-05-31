extends CharacterBody3D
## First-person walker: WASD + mouselook, gravity, collision against the terrain.
## The collision capsule and Camera3D live in player.tscn; this script just drives them.
## (GDScript note: `@export` vars show in the Inspector; `@onready` resolves after the node
## tree is built, so $Camera3D exists by then.)

@export var speed: float = 8.0          # walk speed, m/s
@export var jump_velocity: float = 5.0
@export var mouse_sensitivity: float = 0.0025

@onready var _camera: Camera3D = $Camera3D
var _pitch: float = 0.0                   # accumulated look-up/down, radians
var _compass: Label

func _ready() -> void:
	Input.mouse_mode = Input.MOUSE_MODE_CAPTURED
	_setup_compass()

func _setup_compass() -> void:
	# A simple HUD label at top-center. CanvasLayer keeps it pinned to the screen, not the world.
	var layer := CanvasLayer.new()
	add_child(layer)
	_compass = Label.new()
	_compass.set_anchors_preset(Control.PRESET_TOP_WIDE)
	_compass.offset_top = 12.0
	_compass.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	_compass.add_theme_font_size_override("font_size", 30)
	_compass.add_theme_color_override("font_color", Color.WHITE)
	_compass.add_theme_color_override("font_outline_color", Color.BLACK)
	_compass.add_theme_constant_override("outline_size", 6)
	layer.add_child(_compass)

func _process(_delta: float) -> void:
	# Heading from the way we face. North = -Z, East = +X (matches the map's row0=north).
	var f := -global_transform.basis.z
	var heading := fposmod(rad_to_deg(atan2(f.x, -f.z)), 360.0)
	var dirs := ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
	_compass.text = "%s   %d°" % [dirs[int(round(heading / 45.0)) % 8], int(round(heading))]

func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion and Input.mouse_mode == Input.MOUSE_MODE_CAPTURED:
		rotate_y(-event.relative.x * mouse_sensitivity)          # yaw on the body
		_pitch = clamp(_pitch - event.relative.y * mouse_sensitivity, -1.5, 1.5)
		_camera.rotation.x = _pitch                               # pitch on the camera only
	elif event is InputEventKey and event.pressed and event.keycode == KEY_ESCAPE:
		# Toggle the cursor free/captured so you can quit or click around.
		Input.mouse_mode = (Input.MOUSE_MODE_VISIBLE if Input.mouse_mode == Input.MOUSE_MODE_CAPTURED
			else Input.MOUSE_MODE_CAPTURED)

func _physics_process(delta: float) -> void:
	if not is_on_floor():
		velocity += get_gravity() * delta
	elif Input.is_key_pressed(KEY_SPACE):
		velocity.y = jump_velocity

	# WASD read directly (no InputMap actions needed). Movement is relative to where we face.
	var input_dir := Vector2(
		float(Input.is_key_pressed(KEY_D)) - float(Input.is_key_pressed(KEY_A)),
		float(Input.is_key_pressed(KEY_S)) - float(Input.is_key_pressed(KEY_W)),
	)
	var dir := (transform.basis * Vector3(input_dir.x, 0.0, input_dir.y))
	dir.y = 0.0
	dir = dir.normalized()
	velocity.x = dir.x * speed
	velocity.z = dir.z * speed
	move_and_slide()
