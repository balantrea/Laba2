package main

import "fmt"

type Pet interface {
	getName() string
	makeNoise()
}
type Animal struct {
	name string
}

func NewBaseAnimal(name string) Animal {
	return Animal{name: name}
}
func (b *Animal) getName() string {
	return b.name
}
func (b *Animal) makeNoise() {

}

type Turtle struct {
	Animal
}

func NewTurtle(name string) *Turtle {
	return &Turtle{Animal: NewBaseAnimal(name)}
}
func (t *Turtle) makeNoise() {
	fmt.Println("" +
		"  _____     ____\n" +
		" /      \\  |  o | \n" +
		"|        |/ ___\\| \n" +
		"|_________/     \n" +
		"|_|_| |_|_|")
}

type Parrot struct {
	Animal
}

func NewParrot(name string) *Parrot {
	return &Parrot{Animal: NewBaseAnimal(name)}
}
func (p *Parrot) makeNoise() {
	fmt.Println("" +
		"         /////\n" +
		"     (  - v -  )\n" +
		"  (   )      (    ) \n" +
		"       v.   v")
}

type Person struct {
	pet  Pet
	name string
}

func NewPerson(pet Pet, name string) *Person {
	return &Person{pet: pet, name: name}
}
func (p *Person) setPet(pet Pet) {
	p.pet = pet
}
func (p *Person) getPetInfo() {
	fmt.Println("--------------------")
	fmt.Printf("Pet for %s is named %s\n", p.name, p.pet.getName())
	fmt.Println("And it sounds like this:")
	p.pet.makeNoise()
	fmt.Println("--------------------")
}

func main() {
	parrot := NewParrot("Kesha")
	turtle := NewTurtle("Leo")
	vadim := NewPerson(parrot, "Vadim")
	dima := NewPerson(turtle, "Dima")
	vadim.getPetInfo()
	dima.getPetInfo()
}
