import taichi as ti
import numpy as np
import math
import hub

max_steps = 4096
vis_interval = 256
output_vis_interval = 8
steps = 2048 // 3
assert steps * 2 <= max_steps
head_id = 0
elasticity = 0.0
gravity = -4.8
friction = 2.5
gradient_clip = 1
spring_omega = 10
damping = 15
n_objects = 14
n_springs = 30
n_sin_waves = 10
n_hidden = 32
n_input_states = n_sin_waves + 4 * n_objects + 2

max_num_particle = 256
max_num_spring = 512
particle_mass = 1.0
dt = 0.004
spring_Y = 1000
drag_damping = 1
dashpot_damping = 50
learning_rate = 25
ground_height = 0.1

num_spring = ti.field(int, ())
num_particle = ti.field(int, ())
pos = ti.Vector.field(2, float, max_num_particle)
speed = ti.Vector.field(2, float, max_num_particle)
force = ti.Vector.field(2, float, max_num_particle)
spring_anchor_a = ti.field(int, n_springs)
spring_anchor_b = ti.field(int, n_springs)
rest_length = ti.field(float, (max_num_spring, max_num_spring))

x = ti.Vector.field(2, float)
v = ti.Vector.field(2, float)
v_inc = ti.Vector.field(2, float)
ti.root.dense(ti.l, max_steps).dense(ti.i, n_objects).place(x, v, v_inc)
    
loss = ti.field(float, ())
goal = ti.Vector.field(2, float, ())
spring_length = ti.field(float, n_springs)
spring_stiffness = ti.field(float, n_springs)
spring_actuation = ti.field(float, n_springs)
weights1 = ti.field(float, (n_hidden, n_input_states))
bias1 = ti.field(float, n_hidden)
weights2 = ti.field(float, (n_springs, n_hidden))
bias2 = ti.field(float, n_springs)
hidden = ti.field(float, (max_steps, n_hidden))
center = ti.Vector.field(2, float, max_steps)
act = ti.field(float, (max_steps, n_springs))

ti.root.lazy_grad()


objects = []
springs = []
points = []
point_id = []
mesh_springs = []

def add_object(x):
    objects.append(x)
    return len(objects) - 1

def add_spring(a, b, length=None, stiffness=1, actuation=0.1):
    if length == None:
        length = ((objects[a][0] - objects[b][0])**2 +
                  (objects[a][1] - objects[b][1])**2)**0.5
    springs.append([a, b, length, stiffness, actuation])

def add_mesh_point(i, j):
    if (i, j) not in points:
        id = add_object((i * 0.05 + 0.1, j * 0.05 + 0.1))
        points.append((i, j))
        point_id.append(id)
    return point_id[points.index((i, j))]

def add_mesh_spring(a, b, s, ac):
    if (a, b) in mesh_springs or (b, a) in mesh_springs:
        return

    mesh_springs.append((a, b))
    add_spring(a, b, stiffness=s, actuation=ac)


def add_mesh_square(i, j, actuation=0.0):
    a = add_mesh_point(i, j)
    b = add_mesh_point(i, j + 1)
    c = add_mesh_point(i + 1, j)
    d = add_mesh_point(i + 1, j + 1)

    # b d
    # a c
    add_mesh_spring(a, b, 3e4, actuation)
    add_mesh_spring(c, d, 3e4, actuation)

    for i in [a, b, c, d]:
        for j in [a, b, c, d]:
            if i != j:
                add_mesh_spring(i, j, 3e4, 0)


def add_mesh_triangle(i, j, actuation=0.0):
    a = add_mesh_point(i + 0.5, j + 0.5)
    b = add_mesh_point(i, j + 1)
    d = add_mesh_point(i + 1, j + 1)

    for i in [a, b, d]:
        for j in [a, b, d]:
            if i != j:
                add_mesh_spring(i, j, 3e4, 0)

def setup_robot():
    add_mesh_triangle(2, 0, actuation=0.15)
    add_mesh_triangle(0, 0, actuation=0.15)
    add_mesh_square(0, 1, actuation=0.15)
    add_mesh_square(0, 2,    actuation = 0.15)
    add_mesh_square(1, 2,    actuation = 0.15)
    add_mesh_square(2, 1, actuation=0.15)
    add_mesh_square(2, 2,    actuation = 0.15)

@hub.kernel
def reset() -> int:
    goal[None] = [0.9, 0.2]
    loss[None] = 0
    loss.grad[None] = 1

    setup_robot()
    for i in ti.static(range(n_objects)):
        x[0, i] = objects[i]

    for i in ti.static(range(n_springs)):
        spring_anchor_a[i] = springs[i][0]
        spring_anchor_b[i] = springs[i][1]
        spring_length[i] = springs[i][2]
        spring_stiffness[i] = springs[i][3]
        spring_actuation[i] = springs[i][4]

    return steps

@hub.kernel
def compute_center(t: int):
    for _ in range(1):
        c = ti.Vector([0.0, 0.0])
        for i in ti.static(range(n_objects)):
            c += x[t, i]
        center[t] = (1.0 / n_objects) * c

@hub.grad
def compute_center_grad(t: int):
    for _ in range(1):
        c = ti.Vector([0.0, 0.0])
        for i in ti.static(range(n_objects)):
            c += x[t, i]
        center[t] = (1.0 / n_objects) * c

@hub.kernel
def nn1(t: int):
    for i in range(n_hidden):
        actuation = 0.0
        for j in ti.static(range(n_sin_waves)):
            actuation += weights1[i, j] * ti.sin(spring_omega * t * dt + 2 * math.pi / n_sin_waves * j)
        for j in ti.static(range(n_objects)):
            offset = x[t, j] - center[t]
            # use a smaller weight since there are too many of them
            actuation += weights1[i, j * 4 + n_sin_waves] * offset[0] * 0.05
            actuation += weights1[i, j * 4 + n_sin_waves + 1] * offset[1] * 0.05
            actuation += weights1[i, j * 4 + n_sin_waves + 2] * v[t, j][0] * 0.05
            actuation += weights1[i, j * 4 + n_sin_waves + 3] * v[t, j][1] * 0.05
        actuation += weights1[i, n_objects * 4 + n_sin_waves] * (goal[None][0] - center[t][0])
        actuation += weights1[i, n_objects * 4 + n_sin_waves + 1] * (goal[None][1] - center[t][1])
        actuation += bias1[i]
        actuation = ti.tanh(actuation)
        hidden[t, i] = actuation

@hub.grad
def nn1_grad(t: int):
    for i in range(n_hidden):
        actuation = 0.0
        for j in ti.static(range(n_sin_waves)):
            actuation += weights1[i, j] * ti.sin(spring_omega * t * dt + 2 * math.pi / n_sin_waves * j)
        for j in ti.static(range(n_objects)):
            offset = x[t, j] - center[t]
            # use a smaller weight since there are too many of them
            actuation += weights1[i, j * 4 + n_sin_waves] * offset[0] * 0.05
            actuation += weights1[i, j * 4 + n_sin_waves + 1] * offset[1] * 0.05
            actuation += weights1[i, j * 4 + n_sin_waves + 2] * v[t, j][0] * 0.05
            actuation += weights1[i, j * 4 + n_sin_waves + 3] * v[t, j][1] * 0.05
        actuation += weights1[i, n_objects * 4 + n_sin_waves] * (goal[None][0] - center[t][0])
        actuation += weights1[i, n_objects * 4 + n_sin_waves + 1] * (goal[None][1] - center[t][1])
        actuation += bias1[i]
        actuation = ti.tanh(actuation)
        hidden[t, i] = actuation

@hub.kernel
def nn2(t: int):
    for i in range(n_springs):
        actuation = 0.0
        for j in ti.static(range(n_hidden)):
            actuation += weights2[i, j] * hidden[t, j]
        actuation += bias2[i]
        actuation = ti.tanh(actuation)
        act[t, i] = actuation

@hub.grad
def nn2_grad(t: int):
    for i in range(n_springs):
        actuation = 0.0
        for j in ti.static(range(n_hidden)):
            actuation += weights2[i, j] * hidden[t, j]
        actuation += bias2[i]
        actuation = ti.tanh(actuation)
        act[t, i] = actuation

@hub.kernel
def apply_spring_force(t: int):
    for i in range(n_springs):
        a = spring_anchor_a[i]
        b = spring_anchor_b[i]
        pos_a = x[t, a]
        pos_b = x[t, b]
        dist = pos_a - pos_b
        length = dist.norm() + 1e-4

        target_length = spring_length[i] * (1.0 + spring_actuation[i] * act[t, i])
        impulse = dt * (length - target_length) * spring_stiffness[i] / length * dist

        ti.atomic_add(v_inc[t + 1, a], -impulse)
        ti.atomic_add(v_inc[t + 1, b], impulse)

@hub.grad
def apply_spring_force_grad(t: int):
    for i in range(n_springs):
        a = spring_anchor_a[i]
        b = spring_anchor_b[i]
        pos_a = x[t, a]
        pos_b = x[t, b]
        dist = pos_a - pos_b
        length = dist.norm() + 1e-4

        target_length = spring_length[i] * (1.0 + spring_actuation[i] * act[t, i])
        impulse = dt * (length - target_length) * spring_stiffness[i] / length * dist

        ti.atomic_add(v_inc[t + 1, a], -impulse)
        ti.atomic_add(v_inc[t + 1, b], impulse)

@hub.kernel
def advance_toi(t: int):
    for i in range(n_objects):
        s = ti.exp(-dt * damping)
        old_v = s * v[t - 1, i] + dt * gravity * ti.Vector([0.0, 1.0]) + v_inc[t, i]
        old_x = x[t - 1, i]
        new_x = old_x + dt * old_v
        toi = 0.0
        new_v = old_v
        if new_x[1] < ground_height and old_v[1] < -1e-4:
            toi = -(old_x[1] - ground_height) / old_v[1]
            new_v = ti.Vector([0.0, 0.0])
        new_x = old_x + toi * old_v + (dt - toi) * new_v

        v[t, i] = new_v
        x[t, i] = new_x

@hub.grad
def advance_toi_grad(t: int):
    for i in range(n_objects):
        s = ti.exp(-dt * damping)
        old_v = s * v[t - 1, i] + dt * gravity * ti.Vector([0.0, 1.0]) + v_inc[t, i]
        old_x = x[t - 1, i]
        new_x = old_x + dt * old_v
        toi = 0.0
        new_v = old_v
        if new_x[1] < ground_height and old_v[1] < -1e-4:
            toi = -(old_x[1] - ground_height) / old_v[1]
            new_v = ti.Vector([0.0, 0.0])
        new_x = old_x + toi * old_v + (dt - toi) * new_v

        v[t, i] = new_v
        x[t, i] = new_x

@hub.kernel
def advance_no_toi(t: int):
    for i in range(n_objects):
        s = ti.exp(-dt * damping)
        old_v = s * v[t - 1, i] + dt * gravity * ti.Vector([0.0, 1.0]) + v_inc[t, i]
        old_x = x[t - 1, i]
        new_v = old_v
        depth = old_x[1] - ground_height
        if depth < 0 and new_v[1] < 0:
            # friction projection
            new_v[0] = 0
            new_v[1] = 0
        new_x = old_x + dt * new_v
        v[t, i] = new_v
        x[t, i] = new_x

@hub.grad
def advance_no_toi_grad(t: int):
    for i in range(n_objects):
        s = ti.exp(-dt * damping)
        old_v = s * v[t - 1, i] + dt * gravity * ti.Vector([0.0, 1.0]) + v_inc[t, i]
        old_x = x[t - 1, i]
        new_v = old_v
        depth = old_x[1] - ground_height
        if depth < 0 and new_v[1] < 0:
            # friction projection
            new_v[0] = 0
            new_v[1] = 0
        new_x = old_x + dt * new_v
        v[t, i] = new_v
        x[t, i] = new_x

@hub.kernel
def compute_loss(t: int):
    loss[None] = - x[t, head_id][0]

@hub.grad
def compute_loss_grad(t: int):
    loss[None] = - x[t, head_id][0]

@hub.kernel
def clear_states():
    for t in range(0, max_steps):
        for i in range(0, n_objects):
            v_inc[t, i] = ti.Vector([0.0, 0.0])

@hub.kernel
def render(t: int):
    for i in range(n_objects):
        pos[i] = x[t, i]

@hub.kernel
def clear_gradients():
    loss[None] = 0
    loss.grad[None] = 1
    goal[None] = [0.9, 0.2]
    goal.grad[None] = ti.Vector([0.0, 0.0])
    for i in range(n_hidden):
        for j in range(n_input_states):
            weights1.grad[i, j] = 0.0
        bias1.grad[i] = 0.0
    for i in range(n_springs):
        for j in range(n_hidden):
            weights2.grad[i, j] = 0.0
        bias2.grad[i] = 0.0
    for i in range(max_steps):
        center.grad[i] = ti.Vector([0.0, 0.0])
        for j in range(n_objects):
            x.grad[i, j] = ti.Vector([0.0, 0.0])
            v.grad[i, j] = ti.Vector([0.0, 0.0])
            v_inc.grad[i, j] = ti.Vector([0.0, 0.0])
        for j in range(n_springs):
            act.grad[i, j] = 0.0
        for j in range(n_hidden):
            hidden.grad[i, j] = 0.0
    for i in range(n_springs):
        spring_length.grad[i] = 0.0
        spring_stiffness.grad[i] = 0.0
        spring_actuation.grad[i] = 0.0

@hub.kernel
def optimize():
    for i in range(n_hidden):
        for j in range(n_input_states):
            weights1[i, j] = ti.random() * ti.sqrt(2 / (n_hidden + n_input_states)) * 2

    for i in range(n_springs):
        for j in range(n_hidden):
            # TODO: n_springs should be n_actuators
            weights2[i, j] = ti.random() * ti.sqrt(2 / (n_hidden + n_springs)) * 3
        
@hub.kernel
def optimize1(iter: int) -> float:
    print('Iter=', iter, 'Loss=', loss[None])

    total_norm_sqr = 0.0
    for i in range(n_hidden):
        for j in range(n_input_states):
            total_norm_sqr += weights1.grad[i, j]**2
        total_norm_sqr += bias1.grad[i]**2

    for i in range(n_springs):
        for j in range(n_hidden):
            total_norm_sqr += weights2.grad[i, j]**2
        total_norm_sqr += bias2.grad[i]**2

    print('TNS = ', total_norm_sqr)

    gradient_clip = 0.2
    learning_rate = 25
    ## scale = learning_rate * min(1.0, gradient_clip / total_norm_sqr ** 0.5)
    scale = gradient_clip / (total_norm_sqr**0.5 + 1e-6)
    for i in range(n_hidden):
        for j in range(n_input_states):
            weights1[i, j] -= scale * weights1.grad[i, j]
        bias1[i] -= scale * bias1.grad[i]

    for i in range(n_springs):
        for j in range(n_hidden):
            weights2[i, j] -= scale * weights2.grad[i, j]
        bias2[i] -= scale * bias2.grad[i]

    return loss[None]

hub.bind_particles(pos)
hub.bind_spring_anchors(spring_anchor_a, spring_anchor_b)